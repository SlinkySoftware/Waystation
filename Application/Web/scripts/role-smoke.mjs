// SPDX-License-Identifier: GPL-3.0-or-later
// Minimal-permission smoke (SPEC §4.10, T-U10): signs in as users with few or no permissions and opens every page
// their rail shows, plus pages they may NOT open (which must show the calm "no access" page). Fails on any console
// error, page error, or an API answer of 401/403/5xx (a refused request means the UI asked for something the session
// cannot have). Runs in the official Playwright image (`make ui-smoke`), against
//   - the mock build (default): serves dist/spa under /ui/ itself (build it with `make frontend-build-mock`); the
//     mock users are `nina` (no role), `reed` (reader), `gus` (the anonymous-access grants only) and `grace`
//     (viewer: statistics, no administration), or
//   - a running stack (REAL=1, BASE=http://localhost:8080/ui/): USERS=<comma-separated> users that exist there, all
//     with the password in PASSWORD_FILE (mount it read-only).
// Env: BASE, REAL=1, USERS, PASSWORD_FILE, SETTLE (ms per page, default 1500).
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { extname, join, normalize } from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = require('playwright')

const REAL = process.env.REAL === '1'
const SETTLE = Number(process.env.SETTLE ?? 1500)
const USERS = (process.env.USERS ?? 'nina,reed,gus,grace').split(',').filter(Boolean)
const PASSWORD = REAL ? readFileSync(process.env.PASSWORD_FILE ?? '/run/password', 'utf8').trim() : 'mock-password'
/** Pages no user of this smoke may open (each must answer with the "no access" page, without asking the server). */
const FORBIDDEN_PAGES = ['tasks', 'admin/users', 'admin/audit', 'admin/settings', 'admin/system', 'cleanup']
/** Pages a user without stats.read may not open either. */
const STATS_PAGES = ['activity', 'storage']

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.json': 'application/json', '.woff2': 'font/woff2', '.png': 'image/png', '.ico': 'image/x-icon', '.webmanifest': 'application/manifest+json' }

/** dist/spa under /ui/ with the SPA fallback (what nginx does in the web image). */
function serveDist() {
  const root = join(process.cwd(), 'dist/spa')
  if (!existsSync(join(root, 'index.html'))) throw new Error('dist/spa is missing: run `make frontend-build-mock` first')
  const server = createServer((req, res) => {
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
  })
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)))
}

async function signIn(page, base, username) {
  await page.goto(base + 'sign-in', { waitUntil: 'networkidle' })
  await page.locator('input[autocomplete=username]').fill(username)
  await page.locator('input[autocomplete=current-password]').fill(PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 15000 })
}

async function smoke(browser, base, username) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  const problems = []
  // Not recorded while signing in: before it, the session check answers 401 by design.
  let where = null
  page.on('console', (msg) => {
    if (where && msg.type() === 'error') problems.push(`${where}: console error: ${msg.text()}`)
  })
  page.on('pageerror', (error) => where && problems.push(`${where}: page error: ${error.message}`))
  page.on('response', (response) => {
    const url = new URL(response.url())
    const status = response.status()
    if (where && url.pathname.includes('/api/v1/') && (status === 401 || status === 403 || status >= 500)) {
      problems.push(`${where}: ${response.request().method()} ${url.pathname}${url.search} -> ${status}`)
    }
  })

  await signIn(page, base, username)
  const caps = await page.evaluate(async () => (await (await fetch('/api/v1/auth/me')).json()).capabilities)
  where = 'dashboard'
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.waitForTimeout(SETTLE)
  const rail = await page.locator('nav.rail a.rail__item').evaluateAll((links) => links.map((a) => new URL(a.href).pathname))
  const visited = []
  for (const path of rail) {
    where = path
    await page.goto(new URL(path, base).toString(), { waitUntil: 'networkidle' })
    await page.waitForTimeout(SETTLE)
    if (await page.getByText('No access to this page').count()) problems.push(`${path}: the rail shows a page the user cannot open`)
    visited.push(path)
  }
  if (caps.includes('repositories.browse')) {
    // A repository the user can see: its overview, browse tab, and the settings tab it may not use (→ overview).
    const listed = await page.evaluate(async () => (await (await fetch('/api/v1/repositories?limit=1')).json()).items)
    for (const repo of listed) {
      for (const tab of ['', '/browse', '/settings']) {
        where = `/repositories/${repo.name}${tab}`
        await page.goto(`${base}repositories/${repo.name}${tab}`, { waitUntil: 'networkidle' })
        await page.waitForTimeout(SETTLE)
        visited.push(where)
      }
    }
  }
  const refused = [...FORBIDDEN_PAGES, ...(caps.includes('stats.read') ? [] : STATS_PAGES)]
  for (const path of refused) {
    where = `/${path}`
    await page.goto(base + path, { waitUntil: 'networkidle' })
    await page.waitForTimeout(300)
    if (!(await page.getByText('No access to this page').count())) problems.push(`/${path}: expected the "no access" page`)
  }
  if (!caps.length) {
    where = 'dashboard'
    await page.goto(base, { waitUntil: 'networkidle' })
    if (!(await page.getByText('You are signed in, but have no access yet').count())) problems.push('dashboard: expected the "no access yet" landing')
  }
  // Nothing may keep retrying in the background (the live stream, polling): wait and look again.
  where = 'idle'
  await page.waitForTimeout(SETTLE * 3)
  await context.close()
  return { username, caps, visited, problems }
}

let server = null
let base = process.env.BASE
if (!REAL && !base) {
  server = await serveDist()
  base = `http://127.0.0.1:${server.address().port}/ui/`
}
const browser = await chromium.launch()
let failed = 0
try {
  for (const username of USERS) {
    const result = await smoke(browser, base, username)
    console.log(`${result.username} [${result.caps.join(', ') || 'no capabilities'}]: ${result.visited.join(' ')}`)
    for (const p of result.problems) console.log(`  FAIL ${p}`)
    failed += result.problems.length
  }
} finally {
  await browser.close()
  server?.close()
}
if (failed) {
  console.error(`${failed} problem(s)`)
  process.exit(1)
}
console.log('role smoke: no console errors, no refused requests')
