// SPDX-License-Identifier: GPL-3.0-or-later
// Static server for the mock build (dist/spa under /ui/, SPA fallback: what nginx does in the web image), used by
// the Playwright suites (playwright.config.ts `webServer`). PORT (default 4173).
import { createReadStream, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, normalize } from 'node:path'

const TYPES = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
  '.woff2': 'font/woff2',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.webmanifest': 'application/manifest+json',
}
const root = join(process.cwd(), 'dist/spa')
if (!existsSync(join(root, 'index.html'))) {
  console.error('dist/spa is missing: run `make frontend-build-mock` first')
  process.exit(1)
}
const port = Number(process.env.PORT ?? 4173)
createServer((req, res) => {
  const url = new URL(req.url ?? '/', 'http://localhost')
  if (!url.pathname.startsWith('/ui/')) {
    res.writeHead(302, { Location: '/ui/' }).end()
    return
  }
  const rel = normalize(decodeURIComponent(url.pathname.slice(4))).replace(/^(\.\.(\/|$))+/, '')
  let file = join(root, rel)
  if (!file.startsWith(root) || !existsSync(file) || statSync(file).isDirectory()) file = join(root, 'index.html')
  res.writeHead(200, { 'Content-Type': TYPES[extname(file)] ?? 'application/octet-stream', 'Service-Worker-Allowed': '/ui/' })
  createReadStream(file).pipe(res)
}).listen(port, '127.0.0.1', () => console.log(`mock build on http://127.0.0.1:${port}/ui/`))
