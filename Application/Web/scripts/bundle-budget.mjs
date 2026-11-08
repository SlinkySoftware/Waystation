// SPDX-License-Identifier: GPL-3.0-or-later
// Bundle budget (SPEC §5.1, ARCH §9): the JS needed for the initial route must stay under the limit, gzipped.
// "Initial" = the entry chunk plus every chunk it imports statically (modulepreload links in index.html and the
// entry's static imports), excluding lazily loaded route chunks.
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { gzipSync } from 'node:zlib'

const DIST = process.env.DIST_DIR ?? 'dist/spa'
const LIMIT_KB = Number(process.env.BUNDLE_BUDGET_KB ?? 250)

const html = readFileSync(join(DIST, 'index.html'), 'utf8')
const refs = new Set()
for (const m of html.matchAll(/(?:src|href)="[^"]*?\/?(assets\/[^"]+\.js)"/g)) refs.add(m[1])
// The landing route after sign-in is the dashboard: count its layout and page chunks too.
const ROUTE_CHUNKS = new RegExp(process.env.INITIAL_ROUTE_CHUNKS ?? '^(MainLayout|DashboardPage)-')
for (const f of readdirSync(join(DIST, 'assets'))) if (f.endsWith('.js') && ROUTE_CHUNKS.test(f)) refs.add(`assets/${f}`)

// Follow static imports inside initial chunks (`from"./x.js"` / `import"./x.js"`), not dynamic `import()`.
const queue = [...refs]
while (queue.length) {
  const file = queue.pop()
  const code = readFileSync(join(DIST, file), 'utf8')
  for (const m of code.matchAll(/(?:from|import)\s*"\.\/([^"]+\.js)"/g)) {
    const dep = `assets/${m[1]}`
    if (!refs.has(dep)) {
      refs.add(dep)
      queue.push(dep)
    }
  }
}

let total = 0
const rows = []
for (const file of refs) {
  const size = gzipSync(readFileSync(join(DIST, file)), { level: 9 }).length
  total += size
  rows.push([file, size])
}
rows.sort((a, b) => b[1] - a[1])
for (const [file, size] of rows) console.log(`  ${(size / 1024).toFixed(1).padStart(7)} kB  ${file}`)

const all = readdirSync(join(DIST, 'assets')).filter((f) => f.endsWith('.js'))
const kb = total / 1024
console.log(`Initial route JS: ${kb.toFixed(1)} kB gzip (budget ${LIMIT_KB} kB); ${refs.size} of ${all.length} JS chunks.`)
if (kb > LIMIT_KB) {
  console.error('Bundle budget exceeded.')
  process.exit(1)
}
