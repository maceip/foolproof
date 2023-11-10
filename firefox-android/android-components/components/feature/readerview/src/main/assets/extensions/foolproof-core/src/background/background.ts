export {};
    import { CID } from 'multiformats/cid'

import { unixfs } from '@helia/unixfs'
import { createHelia } from 'helia'

console.log("Add and import any TypeScript you like in here");
(async () => {

const helia = await createHelia()
const fs = unixfs(helia)

const cat = async (cid:CID) => {
    const decoder = new TextDecoder()
    let content = ''

    for await (const chunk of fs.cat(cid)) {
      content += decoder.decode(chunk, {
        stream: true
      })
    }

    return content
  }

  const store = async (name:string, content:any) => {
    const id = helia.libp2p.peerId
    console.log(`Helia node peer ID ${id}` )

    const fileToAdd = {
      path: `${name}`,
      content: new TextEncoder().encode(content)
    }

    console.log(`Adding file ${fileToAdd.path}`)
    const cid = await fs.addFile(fileToAdd)

    console.log(`Added ${cid}`, cid)
    console.log('Reading file', )

    const text = await cat(cid)

    console.log(`\u2514\u2500 ${name} ${text.toString()}`)
    console.log(`Preview: <a href="https://ipfs.io/ipfs/${cid}">https://ipfs.io/ipfs/${cid}</a>`, )
  }

  setInterval(() => {
    let peers = ''

    for (const connection of helia.libp2p.getConnections()) {
      peers += `${connection.remotePeer.toString()}\n`
    }

    if (peers === '') {
      peers = 'Not connected to any peers'
    }


    let dialQueue = ''

    for (const dial of helia.libp2p.getDialQueue()) {
      dialQueue += `${dial.peerId} - ${dial.status}\n${dial.multiaddrs.map(ma => ma.toString()).join('\n')}\n`
    }

    if (dialQueue === '') {
      dialQueue = 'Dial queue empty'
    }


    let multiaddrs = ''

    for (const ma of helia.libp2p.getMultiaddrs()) {
      multiaddrs += `${ma.toString()}\n`
    }

    if (multiaddrs === '') {
      multiaddrs = 'Not listening on any addresses'
    }

  }, 500)
})();