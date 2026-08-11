import { createReadStream } from 'node:fs'
import { stat } from 'node:fs/promises'
import { createServer } from 'node:http'
import { extname, resolve, sep } from 'node:path'

const dist = resolve(import.meta.dirname, '../dist')
const types = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
}

createServer(async (request, response) => {
  const path = new URL(request.url ?? '/', 'http://127.0.0.1').pathname
  if (path === '/config.json') {
    response.writeHead(200, { 'content-type': types['.json'] })
    response.end(JSON.stringify({ dataMode: 'demo', apexOrigin: '', langgraphOrigin: '' }))
    return
  }
  const requested = resolve(dist, `.${path}`)
  let file = requested.startsWith(`${dist}${sep}`) ? requested : resolve(dist, 'index.html')
  try {
    if ((await stat(file)).isDirectory()) file = resolve(file, 'index.html')
  } catch {
    file = resolve(dist, 'index.html')
  }
  response.writeHead(200, { 'content-type': types[extname(file)] ?? 'application/octet-stream' })
  createReadStream(file).pipe(response)
}).listen(4174, '127.0.0.1')
