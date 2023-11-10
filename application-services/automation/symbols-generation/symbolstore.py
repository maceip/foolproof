#!/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Usage: symbolstore.py <params> <dump_syms path> <symbol store path>
#                                <debug info files or dirs>
#   Runs dump_syms on each debug info file specified on the command line,
#   then places the resulting symbol file in the proper directory
#   structure in the symbol store path.  Accepts multiple files
#   on the command line, so can be called as part of a pipe using
#   find <dir> | xargs symbolstore.pl <dump_syms> <storepath>
#   But really, you might just want to pass it <dir>.
#
#   Parameters accepted:
#     -c           : Copy debug info files to the same directory structure
#                    as sym files. On Windows, this will also copy
#                    binaries into the symbol store.
#     -a "<archs>" : Run dump_syms -a <arch> for each space separated
#                    cpu architecture in <archs> (only on OS X)
#     -s <srcdir>  : Use <srcdir> as the top source directory to
#                    generate relative filenames.

from __future__ import print_function

import errno
import sys
import platform
import io
import os
import re
import shutil
import textwrap
import subprocess
import time
import ctypes

from optparse import OptionParser

# Utility classes

class VCSFileInfo:
    """ A base class for version-controlled file information. Ensures that the
        following attributes are generated only once (successfully):

            self.root
            self.clean_root
            self.revision
            self.filename

        The attributes are generated by a single call to the GetRoot,
        GetRevision, and GetFilename methods. Those methods are explicitly not
        implemented here and must be implemented in derived classes. """

    def __init__(self, file):
        if not file:
            raise ValueError
        self.file = file

    def __getattr__(self, name):
        """ __getattr__ is only called for attributes that are not set on self,
            so setting self.[attr] will prevent future calls to the GetRoot,
            GetRevision, and GetFilename methods. We don't set the values on
            failure on the off chance that a future call might succeed. """

        if name == "root":
            root = self.GetRoot()
            if root:
                self.root = root
            return root

        elif name == "clean_root":
            clean_root = self.GetCleanRoot()
            if clean_root:
                self.clean_root = clean_root
            return clean_root

        elif name == "revision":
            revision = self.GetRevision()
            if revision:
                self.revision = revision
            return revision

        elif name == "filename":
            filename = self.GetFilename()
            if filename:
                self.filename = filename
            return filename

        raise AttributeError

    def GetRoot(self):
        """ This method should return the unmodified root for the file or 'None'
            on failure. """
        raise NotImplementedError

    def GetCleanRoot(self):
        """ This method should return the repository root for the file or 'None'
            on failure. """
        raise NotImplementedError

    def GetRevision(self):
        """ This method should return the revision number for the file or 'None'
            on failure. """
        raise NotImplementedError

    def GetFilename(self):
        """ This method should return the repository-specific filename for the
            file or 'None' on failure. """
        raise NotImplementedError

# This regex finds out the org and the repo from a git remote URL.
githubRegex = re.compile(r'^(?:https://github.com/|git@github.com:)([^/]+)/([^/]+?)(?:.git)?$')

def read_output(*args):
    (stdout, _) = subprocess.Popen(args=args, stdout=subprocess.PIPE).communicate()
    return stdout.decode("utf-8").rstrip()

class GitHubRepoInfo:
    """
    Info about a locally cloned Git repository that has its "origin" remote on GitHub.
    """
    def __init__(self, path):
        self.path = path
        if 'APPSERVICES_HEAD_REPOSITORY' in os.environ:
            remote_url = os.environ['APPSERVICES_HEAD_REPOSITORY']
        else:
            remote_url = read_output('git', '-C', path, 'remote', 'get-url', 'origin')
        match = githubRegex.match(remote_url)
        if match is None:
            print(textwrap.dedent("""\
            Could not determine repo info for %s (%s).  This is probably because
            the repo is not one that was cloned from a GitHub remote.""") % (path), file=sys.stderr)
            sys.exit(1)
        (org, repo) = match.groups()
        cleanroot = "github.com/%s/%s" % (org, repo)

        # Try to get a tag if possible, otherwise get a git hash.
        rev = None
        p = subprocess.Popen(args=['git', '-C', path, 'name-rev', '--name-only', '--tags', 'HEAD', '--no-undefined'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (stdout, _) = p.communicate()
        if p.returncode == 0:
            rev = stdout.decode("utf-8").rstrip()
        else:
            rev = read_output('git', '-C', path, 'rev-parse', 'HEAD')

        root = "https://raw.githubusercontent.com/%s/%s/%s/" % (org, repo, rev)

        self.rev = rev
        self.root = root
        self.cleanroot = cleanroot

    def GetFileInfo(self, file):
        return GitFileInfo(file, self)

class GitFileInfo(VCSFileInfo):
    def __init__(self, file, repo):
        VCSFileInfo.__init__(self, file)
        self.repo = repo
        self.file = os.path.relpath(file, repo.path)

    # Root is used by for source server indexing
    def GetRoot(self):
        return self.repo.path

    # Cleanroot is used for filenames
    def GetCleanRoot(self):
        return self.repo.cleanroot

    def GetRevision(self):
        return self.repo.rev

    def GetFilename(self):
        if self.revision and self.clean_root:
            return "git:%s:%s:%s" % (self.clean_root, self.file, self.revision)
        return self.file

# Utility functions

# A cache of files for which VCS info has already been determined. Used to
# prevent extra filesystem activity or process launching.
vcsFileInfoCache = {}

if platform.system() == 'Windows':
    def normpath(path):
        '''
        Normalize a path using `GetFinalPathNameByHandleW` to get the
        path with all components in the case they exist in on-disk, so
        that making links to a case-sensitive server (hg.mozilla.org) works.

        This function also resolves any symlinks in the path.
        '''
        # Return the original path if something fails, which can happen for paths that
        # don't exist on this system (like paths from the CRT).
        result = path

        ctypes.windll.kernel32.SetErrorMode(ctypes.c_uint(1))
        if not isinstance(path, unicode):
            path = unicode(path, sys.getfilesystemencoding())
        handle = ctypes.windll.kernel32.CreateFileW(path,
                                                    # GENERIC_READ
                                                    0x80000000,
                                                    # FILE_SHARE_READ
                                                    1,
                                                    None,
                                                    # OPEN_EXISTING
                                                    3,
                                                    # FILE_FLAG_BACKUP_SEMANTICS
                                                    # This is necessary to open
                                                    # directory handles.
                                                    0x02000000,
                                                    None)
        if handle != -1:
            size = ctypes.windll.kernel32.GetFinalPathNameByHandleW(handle,
                                                                    None,
                                                                    0,
                                                                    0)
            buf = ctypes.create_unicode_buffer(size)
            if ctypes.windll.kernel32.GetFinalPathNameByHandleW(handle,
                                                                buf,
                                                                size,
                                                                0) > 0:
                # The return value of GetFinalPathNameByHandleW uses the
                # '\\?\' prefix.
                result = buf.value.encode(sys.getfilesystemencoding())[4:]
            ctypes.windll.kernel32.CloseHandle(handle)
        return result
else:
    # Just use the os.path version otherwise.
    normpath = os.path.normpath

def IsInDir(file, dir):
    # the lower() is to handle win32+vc8, where
    # the source filenames come out all lowercase,
    # but the srcdir can be mixed case
    return os.path.abspath(file).lower().startswith(os.path.abspath(dir).lower())

def GetVCSFilenameFromSrcdir(file, srcdir):
    if srcdir not in Dumper.srcdirRepoInfo:
        # Not in cache, so find it and cache it
        if os.path.isdir(os.path.join(srcdir, '.git')):
            Dumper.srcdirRepoInfo[srcdir] = GitHubRepoInfo(srcdir)
        else:
            # Unknown VCS or file is not in a repo.
            return None
    return Dumper.srcdirRepoInfo[srcdir].GetFileInfo(file)

def GetVCSFilename(file, srcdirs):
    """Given a full path to a file, and the top source directory,
    look for version control information about this file, and return
    a tuple containing
    1) a specially formatted filename that contains the VCS type,
    VCS location, relative filename, and revision number, formatted like:
    vcs:vcs location:filename:revision
    For example:
    cvs:cvs.mozilla.org/cvsroot:mozilla/browser/app/nsBrowserApp.cpp:1.36
    2) the unmodified root information if it exists"""
    (path, filename) = os.path.split(file)
    if path == '' or filename == '':
        return (file, None)

    fileInfo = None
    root = ''
    if file in vcsFileInfoCache:
        # Already cached this info, use it.
        fileInfo = vcsFileInfoCache[file]
    else:
        for srcdir in srcdirs:
            if not IsInDir(file, srcdir):
                continue
            fileInfo = GetVCSFilenameFromSrcdir(file, srcdir)
            if fileInfo:
                vcsFileInfoCache[file] = fileInfo
                break

    if fileInfo:
        file = fileInfo.filename
        root = fileInfo.root

    # we want forward slashes on win32 paths
    return (file.replace("\\", "/"), root)


def GetPlatformSpecificDumper(**kwargs):
    """This function simply returns a instance of a subclass of Dumper
    that is appropriate for the current platform."""
    return {'WINNT': Dumper_Win32,
            'Linux': Dumper_Linux,
            'Darwin': Dumper_Mac}[platform.system()](**kwargs)

# Git source indexing cargo culted from https://gist.github.com/baldurk/c6feb31b0305125c6d1a
def SourceIndex(fileStream, outputPath, vcs_root):
    """Takes a list of files, writes info to a data block in a .stream file"""
    # Creates a .pdb.stream file in the mozilla\objdir to be used for source indexing
    # Create the srcsrv data block that indexes the pdb file
    result = True
    pdbStreamFile = open(outputPath, "w")
    pdbStreamFile.write('''SRCSRV: ini ------------------------------------------------\r\nVERSION=2\r\nINDEXVERSION=2\r\nVERCTRL=http\r\nSRCSRV: variables ------------------------------------------\r\nHTTP_ALIAS=''')
    pdbStreamFile.write(vcs_root)
    pdbStreamFile.write('''\r\nHTTP_EXTRACT_TARGET=%HTTP_ALIAS%/%var3%/%var2%\r\nSRCSRVTRG=%http_extract_target%\r\nSRCSRV: source files ---------------------------------------\r\n''')
    pdbStreamFile.write(fileStream) # can't do string interpolation because the source server also uses this and so there are % in the above
    pdbStreamFile.write("SRCSRV: end ------------------------------------------------\r\n\n")
    pdbStreamFile.close()
    return result


class Dumper:
    """This class can dump symbols from a file with debug info, and
    store the output in a directory structure that is valid for use as
    a Breakpad symbol server.  Requires a path to a dump_syms binary--
    |dump_syms| and a directory to store symbols in--|symbol_path|.
    Optionally takes a list of processor architectures to process from
    each debug file--|archs|, the full path to the top source
    directory--|srcdir|, for generating relative source file names,
    and an option to copy debug info files alongside the dumped
    symbol files--|copy_debug|, mostly useful for creating a
    Microsoft Symbol Server from the resulting output.

    You don't want to use this directly if you intend to process files.
    Instead, call GetPlatformSpecificDumper to get an instance of a
    subclass."""
    srcdirRepoInfo = {}

    def __init__(self, dump_syms, symbol_path,
                 archs=None,
                 srcdirs=[],
                 copy_debug=False,
                 vcsinfo=False,
                 srcsrv=False,
                 file_mapping=None):
        # popen likes absolute paths, at least on windows
        self.dump_syms = os.path.abspath(dump_syms)
        self.symbol_path = symbol_path
        if archs is None:
            # makes the loop logic simpler
            self.archs = ['']
        else:
            self.archs = ['-a %s' % a for a in archs.split()]
        # Any paths that get compared to source file names need to go through normpath.
        self.srcdirs = [normpath(s) for s in srcdirs]
        self.copy_debug = copy_debug
        self.vcsinfo = vcsinfo
        self.srcsrv = srcsrv
        self.file_mapping = file_mapping or {}

    # subclasses override this
    def ShouldProcess(self, file):
        return True

    def RunFileCommand(self, file):
        """Utility function, returns the output of file(1)"""
        # we use -L to read the targets of symlinks,
        # and -b to print just the content, not the filename
        return read_output('file', '-Lb', file)

    # This is a no-op except on Win32
    def SourceServerIndexing(self, debug_file, guid, sourceFileStream, vcs_root):
        return ""

    # subclasses override this if they want to support this
    def CopyDebug(self, file, debug_file, guid, code_file, code_id):
        pass

    def Process(self, file_to_process):
        """Process the given file."""
        if self.ShouldProcess(os.path.abspath(file_to_process)):
            self.ProcessFile(file_to_process)
        else:
            print("Cannot process file %s. Skipping." % file_to_process)

    def ProcessFile(self, file, dsymbundle=None):
        """Dump symbols from these files into a symbol file, stored
        in the proper directory structure in  |symbol_path|; processing is performed
        asynchronously, and Finish must be called to wait for it complete and cleanup.
        All files after the first are fallbacks in case the first file does not process
        successfully; if it does, no other files will be touched."""
        print("Beginning work for file: %s" % file, file=sys.stderr)
        for arch_num, arch in enumerate(self.archs):
            self.ProcessFileWork(file, arch_num, arch, None, dsymbundle)

    def dump_syms_cmdline(self, file, arch, dsymbundle=None):
        '''
        Get the commandline used to invoke dump_syms.
        '''
        # The Mac dumper overrides this.
        return [self.dump_syms, file]

    def ProcessFileWork(self, file, arch_num, arch, vcs_root, dsymbundle=None):
        t_start = time.time()
        print("Processing file: %s" % file, file=sys.stderr)

        sourceFileStream = ''
        code_id, code_file = None, None
        try:
            cmd = self.dump_syms_cmdline(file, arch, dsymbundle=dsymbundle)
            print(' '.join(cmd), file=sys.stderr)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=open(os.devnull, 'wb'))
            stdout = io.TextIOWrapper(proc.stdout, encoding="utf-8")
            module_line = stdout.readline()
            if module_line.startswith("MODULE"):
                # MODULE os cpu guid debug_file
                (guid, debug_file) = (module_line.split())[3:5]
                # strip off .pdb extensions, and append .sym
                sym_file = re.sub("\.pdb$", "", debug_file) + ".sym"
                # we do want forward slashes here
                rel_path = os.path.join(debug_file,
                                        guid,
                                        sym_file).replace("\\", "/")
                full_path = os.path.normpath(os.path.join(self.symbol_path,
                                                          rel_path))
                try:
                    os.makedirs(os.path.dirname(full_path))
                except OSError: # already exists
                    pass
                f = open(full_path, "w")
                f.write(module_line)
                # now process the rest of the output
                for line in stdout:
                    if line.startswith("FILE"):
                        # FILE index filename
                        (x, index, filename) = line.rstrip().split(None, 2)
                        # We want original file paths for the source server.
                        sourcepath = filename
                        filename = normpath(filename)
                        if filename in self.file_mapping:
                            filename = self.file_mapping[filename]
                        if self.vcsinfo:
                            (filename, rootname) = GetVCSFilename(filename, self.srcdirs)
                            # sets vcs_root in case the loop through files were to end on an empty rootname
                            if vcs_root is None:
                              if rootname:
                                 vcs_root = rootname
                        # gather up files with git for indexing
                        if filename.startswith("git"):
                            (vcs, checkout, source_file, revision) = filename.split(":", 3)
                            # Contrary to HG we do not include the revision as it is part of the
                            # repo URL.
                            sourceFileStream += sourcepath + "*" + source_file + "\r\n"
                        f.write("FILE %s %s\n" % (index, filename))
                    elif line.startswith("INFO CODE_ID "):
                        # INFO CODE_ID code_id code_file
                        # This gives some info we can use to
                        # store binaries in the symbol store.
                        bits = line.rstrip().split(None, 3)
                        if len(bits) == 4:
                            code_id, code_file = bits[2:]
                        f.write(line)
                    else:
                        # pass through all other lines unchanged
                        f.write(line)
                f.close()
                retcode = proc.wait()
                if retcode != 0:
                    raise RuntimeError(
                        "dump_syms failed with error code %d" % retcode)
                # we output relative paths so callers can get a list of what
                # was generated
                print(rel_path)
                if self.srcsrv and vcs_root:
                    # add source server indexing to the pdb file
                    self.SourceServerIndexing(debug_file, guid, sourceFileStream, vcs_root)
                # only copy debug the first time if we have multiple architectures
                if self.copy_debug and arch_num == 0:
                    self.CopyDebug(file, debug_file, guid,
                                   code_file, code_id)
        except StopIteration:
            pass
        except Exception as e:
            print("Unexpected error: %s" % str(e), file=sys.stderr)
            raise

        if dsymbundle:
            shutil.rmtree(dsymbundle)

        elapsed = time.time() - t_start
        print('Finished processing %s in %.2fs' % (file, elapsed),
              file=sys.stderr)

# Platform-specific subclasses.  For the most part, these just have
# logic to determine what files to extract symbols from.

def locate_pdb(path):
    '''Given a path to a binary, attempt to locate the matching pdb file with simple heuristics:
    * Look for a pdb file with the same base name next to the binary
    * Look for a pdb file with the same base name in the cwd

    Returns the path to the pdb file if it exists, or None if it could not be located.
    '''
    path, ext = os.path.splitext(path)
    pdb = path + '.pdb'
    if os.path.isfile(pdb):
        return pdb
    # If there's no pdb next to the file, see if there's a pdb with the same root name
    # in the cwd. We build some binaries directly into dist/bin, but put the pdb files
    # in the relative objdir, which is the cwd when running this script.
    base = os.path.basename(pdb)
    pdb = os.path.join(os.getcwd(), base)
    if os.path.isfile(pdb):
        return pdb
    return None

class Dumper_Win32(Dumper):
    fixedFilenameCaseCache = {}

    def ShouldProcess(self, file):
        """This function will allow processing of exe or dll files that have pdb
        files with the same base name next to them."""
        if file.endswith(".exe") or file.endswith(".dll"):
            if locate_pdb(file) is not None:
                return True
        return False


    def CopyDebug(self, file, debug_file, guid, code_file, code_id):
        file = locate_pdb(file)
        def compress(path):
            compressed_file = path[:-1] + '_'
            # ignore makecab's output
            makecab = os.environ['MAKECAB']
            success = subprocess.call([makecab, "-D",
                                       "CompressionType=MSZIP",
                                       path, compressed_file],
                                      stdout=open(os.devnull, 'w'),
                                      stderr=subprocess.STDOUT)
            if success == 0 and os.path.exists(compressed_file):
                os.unlink(path)
                return True
            return False

        rel_path = os.path.join(debug_file,
                                guid,
                                debug_file).replace("\\", "/")
        full_path = os.path.normpath(os.path.join(self.symbol_path,
                                                  rel_path))
        shutil.copyfile(file, full_path)
        if compress(full_path):
            print(rel_path[:-1] + '_')
        else:
            print(rel_path)

        # Copy the binary file as well
        if code_file and code_id:
            full_code_path = os.path.join(os.path.dirname(file),
                                          code_file)
            if os.path.exists(full_code_path):
                rel_path = os.path.join(code_file,
                                        code_id,
                                        code_file).replace("\\", "/")
                full_path = os.path.normpath(os.path.join(self.symbol_path,
                                                          rel_path))
                try:
                    os.makedirs(os.path.dirname(full_path))
                except OSError as e:
                    if e.errno != errno.EEXIST:
                        raise
                shutil.copyfile(full_code_path, full_path)
                if compress(full_path):
                    print(rel_path[:-1] + '_')
                else:
                    print(rel_path)

    def SourceServerIndexing(self, debug_file, guid, sourceFileStream, vcs_root):
        # Creates a .pdb.stream file in the mozilla\objdir to be used for source indexing
        streamFilename = debug_file + ".stream"
        stream_output_path = os.path.abspath(streamFilename)
        # Call SourceIndex to create the .stream file
        result = SourceIndex(sourceFileStream, stream_output_path, vcs_root)
        if self.copy_debug:
            pdbstr_path = os.environ.get("PDBSTR_PATH")
            pdbstr = os.path.normpath(pdbstr_path)
            subprocess.call([pdbstr, "-w", "-p:" + os.path.basename(debug_file),
                             "-i:" + os.path.basename(streamFilename), "-s:srcsrv"],
                            cwd=os.path.dirname(stream_output_path))
            # clean up all the .stream files when done
            os.remove(stream_output_path)
        return result


class Dumper_Linux(Dumper):
    objcopy = os.environ['OBJCOPY'] if 'OBJCOPY' in os.environ else 'objcopy'
    def ShouldProcess(self, file):
        """This function will allow processing of files that are
        executable, or end with the .so extension, and additionally
        file(1) reports as being ELF files.  It expects to find the file
        command in PATH."""
        if file.endswith(".so") or os.access(file, os.X_OK):
            return self.RunFileCommand(file).startswith("ELF")
        return False

    def CopyDebug(self, file, debug_file, guid, code_file, code_id):
        # We want to strip out the debug info, and add a
        # .gnu_debuglink section to the object, so the debugger can
        # actually load our debug info later.
        # In some odd cases, the object might already have an irrelevant
        # .gnu_debuglink section, and objcopy doesn't want to add one in
        # such cases, so we make it remove it any existing one first.
        file_dbg = file + ".dbg"
        if subprocess.call([self.objcopy, '--only-keep-debug', file, file_dbg]) == 0 and \
           subprocess.call([self.objcopy, '--remove-section', '.gnu_debuglink',
                            '--add-gnu-debuglink=%s' % file_dbg, file]) == 0:
            rel_path = os.path.join(debug_file,
                                    guid,
                                    debug_file + ".dbg")
            full_path = os.path.normpath(os.path.join(self.symbol_path,
                                                      rel_path))
            shutil.move(file_dbg, full_path)
            # gzip the shipped debug files
            os.system("gzip -4 -f %s" % full_path)
            print(rel_path + ".gz")
        else:
            if os.path.isfile(file_dbg):
                os.unlink(file_dbg)

class Dumper_Mac(Dumper):
    def ShouldProcess(self, file):
        """This function will allow processing of files that are
        executable, or end with the .dylib extension, and additionally
        file(1) reports as being Mach-O files.  It expects to find the file
        command in PATH."""
        if file.endswith(".dylib") or os.access(file, os.X_OK):
            return self.RunFileCommand(file).startswith("Mach-O")
        return False

    def ProcessFile(self, file):
        print("Starting Mac pre-processing on file: %s" % file,
              file=sys.stderr)
        dsymbundle = self.GenerateDSYM(file)
        if dsymbundle:
            # kick off new jobs per-arch with our new list of files
            Dumper.ProcessFile(self, file, dsymbundle=dsymbundle)

    def dump_syms_cmdline(self, file, arch, dsymbundle=None):
        '''
        Get the commandline used to invoke dump_syms.
        '''
        # dump_syms wants the path to the original binary and the .dSYM
        # in order to dump all the symbols.
        if dsymbundle:
            # This is the .dSYM bundle.
            return [self.dump_syms] + arch.split() + ['-g', dsymbundle, file]
        return Dumper.dump_syms_cmdline(self, file, arch)

    def GenerateDSYM(self, file):
        """dump_syms on Mac needs to be run on a dSYM bundle produced
        by dsymutil(1), so run dsymutil here and pass the bundle name
        down to the superclass method instead."""
        t_start = time.time()
        print("Running Mac pre-processing on file: %s" % (file,),
              file=sys.stderr)

        dsymbundle = file + ".dSYM"
        if os.path.exists(dsymbundle):
            shutil.rmtree(dsymbundle)
        # dsymutil takes --arch=foo instead of -a foo like everything else
        try:
            cmd = (["dsymutil"] +
                   [a.replace('-a ', '--arch=') for a in self.archs if a] +
                   [file])
            print(' '.join(cmd), file=sys.stderr)
            subprocess.check_call(cmd, stdout=open(os.devnull, 'w'))
        except subprocess.CalledProcessError as e:
            print('Error running dsymutil: %s' % str(e), file=sys.stderr)
            raise

        if not os.path.exists(dsymbundle):
            # dsymutil won't produce a .dSYM for files without symbols
            print("No symbols found in file: %s" % (file,), file=sys.stderr)
            return False

        elapsed = time.time() - t_start
        print('Finished processing %s in %.2fs' % (file, elapsed),
              file=sys.stderr)
        return dsymbundle

    def CopyDebug(self, file, debug_file, guid, code_file, code_id):
        """ProcessFile has already produced a dSYM bundle, so we should just
        copy that to the destination directory. However, we'll package it
        into a .tar.bz2 because the debug symbols are pretty huge, and
        also because it's a bundle, so it's a directory. |file| here is the
        the original filename."""
        dsymbundle = file + '.dSYM'
        rel_path = os.path.join(debug_file,
                                guid,
                                os.path.basename(dsymbundle) + ".tar.bz2")
        full_path = os.path.abspath(os.path.join(self.symbol_path,
                                                  rel_path))
        success = subprocess.call(["tar", "cjf", full_path, os.path.basename(dsymbundle)],
                                  cwd=os.path.dirname(dsymbundle),
                                  stdout=open(os.devnull, 'w'), stderr=subprocess.STDOUT)
        if success == 0 and os.path.exists(full_path):
            print(rel_path)

# Entry point if called as a standalone program
def main():
    parser = OptionParser(usage="usage: %prog [options] <dump_syms binary> <symbol store path> <debug info files>")
    parser.add_option("-c", "--copy",
                      action="store_true", dest="copy_debug", default=False,
                      help="Copy debug info files into the same directory structure as symbol files")
    parser.add_option("-a", "--archs",
                      action="store", dest="archs",
                      help="Run dump_syms -a <arch> for each space separated cpu architecture in ARCHS (only on OS X)")
    parser.add_option("-s", "--srcdir",
                      action="append", dest="srcdir", default=[],
                      help="Use SRCDIR to determine relative paths to source files")
    parser.add_option("-v", "--vcs-info",
                      action="store_true", dest="vcsinfo",
                      help="Try to retrieve VCS info for each FILE listed in the output")
    parser.add_option("-i", "--source-index",
                      action="store_true", dest="srcsrv", default=False,
                      help="Add source index information to debug files, making them suitable for use in a source server.")
    (options, args) = parser.parse_args()

    #check to see if the pdbstr.exe exists
    if options.srcsrv:
        pdbstr = os.environ.get("PDBSTR_PATH")
        if not os.path.exists(pdbstr):
            print("Invalid path to pdbstr.exe - please set/check PDBSTR_PATH.\n", file=sys.stderr)
            sys.exit(1)

    if len(args) < 3:
        parser.error("not enough arguments")
        exit(1)

    dumper = GetPlatformSpecificDumper(dump_syms=args[0],
                                       symbol_path=args[1],
                                       copy_debug=options.copy_debug,
                                       archs=options.archs,
                                       srcdirs=options.srcdir,
                                       vcsinfo=options.vcsinfo,
                                       srcsrv=options.srcsrv)

    dumper.Process(args[2])

# run main if run directly
if __name__ == "__main__":
    main()
