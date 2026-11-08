// SPDX-License-Identifier: GPL-3.0-or-later
// Screenshot tour (both themes, desktop + mobile) for review. Runs in the official Playwright image on the host
// network (so http://localhost is a secure context), against either
//   - the mock UI: a running `npm run dev:mock` (or a mock build); the MSW service worker supplies the data, or
//   - a REAL stack (REAL=1): signs in through the form with ADMIN_PASSWORD_FILE and shows what the server has
//     cached (run `make e2e-m0` first so docker, npm, apt and raw content exists). This is how the M0 tour
//     (Documents/Notes/UI_TOUR_M0.md) is taken.
//   docker run --rm --network host -v "$PWD:/src" -w /src/Application/Web mcr.microsoft.com/playwright:v1.65.0-noble \
//     sh -c 'npm i --no-save --prefix /tmp/pw playwright@1.65.0 >/dev/null && NODE_PATH=/tmp/pw/node_modules node scripts/screenshots.mjs'
// Env: BASE (default http://localhost:9173/ui/, REAL: http://localhost:8080/ui/), OUT (default
// ../../Documents/Notes/screenshots/m0), ONLY (regex on shot name), REAL=1, ADMIN_USER (default admin),
// ADMIN_PASSWORD_FILE (REAL only; mount it read-only into the container).
import { mkdirSync, readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { join } from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = require('playwright')

const REAL = process.env.REAL === '1'
const BASE = process.env.BASE ?? (REAL ? 'http://localhost:8080/ui/' : 'http://localhost:9173/ui/')
const ADMIN_USER = process.env.ADMIN_USER ?? 'admin'
const ADMIN_PASSWORD = REAL ? readFileSync(process.env.ADMIN_PASSWORD_FILE ?? '/run/adminpw', 'utf8').trim() : ''
const OUT = process.env.OUT ?? '../../Documents/Notes/screenshots/m0'
const ONLY = process.env.ONLY ? new RegExp(process.env.ONLY) : null
const WIDTHS = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } }
const THEMES = ['light', 'dark']

/** name, path, signed in?, optional action before the shot, settle time (ms), full page? */
const MOCK_SHOTS = [
  { name: 'sign-in', path: 'sign-in', auth: false },
  { name: 'dashboard', path: '', settle: 7000, full: true, fullMobile: true },
  { name: 'repositories', path: 'repositories', full: true },
  { name: 'create-repository', path: 'repositories', action: createFlow },
  { name: 'repo-dockerhub-proxy-setup', path: 'repositories/dockerhub-proxy', action: openSetup },
  { name: 'repo-ghcr-proxy-setup', path: 'repositories/ghcr-proxy', action: openSetup },
  { name: 'repo-npm-setup', path: 'repositories/npm', action: openSetup },
  { name: 'repo-debian-overview', path: 'repositories/debian', settle: 2500, full: true },
  { name: 'repo-rocky-setup', path: 'repositories/rocky', action: openSetup },
  { name: 'browse-docker-tags', path: 'repositories/dockerhub-proxy/browse?image=library%2Fnginx', full: true },
  { name: 'browse-docker-manifest', path: 'repositories/dockerhub-proxy/browse?image=library%2Fnginx&ref=1.29.3', full: true },
  { name: 'browse-npm-versions', path: 'repositories/npm/browse?package=typescript', full: true },
  { name: 'browse-apt-packages', path: 'repositories/debian/browse?suite=trixie', full: true },
  { name: 'browse-yum', path: 'repositories/rocky/browse' },
  { name: 'browse-raw', path: 'repositories/raw-releases/browse?path=docker%2Fcompose%2Freleases%2Fdownload%2Fv2.40.3', full: true },
  { name: 'repo-settings', path: 'repositories/ghcr-proxy/settings', full: true },
]

/** The real-server tour: the demo repositories and the content `make e2e-m0` caches. */
const REAL_SHOTS = [
  { name: 'sign-in', path: 'sign-in', auth: false },
  { name: 'dashboard', path: '', settle: 7000, full: true, fullMobile: true },
  { name: 'repositories', path: 'repositories', full: true },
  { name: 'create-repository', path: 'repositories', action: createFlow },
  { name: 'repo-dockerhub-proxy-setup', path: 'repositories/dockerhub-proxy', action: openSetup },
  { name: 'repo-npm-setup', path: 'repositories/npm', action: openSetup },
  { name: 'repo-debian-setup', path: 'repositories/debian', action: openSetup },
  { name: 'repo-raw-releases-setup', path: 'repositories/raw-releases', action: openSetup },
  { name: 'repo-debian-overview', path: 'repositories/debian', settle: 2500, full: true },
  { name: 'browse-docker-images', path: 'repositories/dockerhub-proxy/browse', full: true },
  { name: 'browse-docker-tags', path: 'repositories/dockerhub-proxy/browse?image=library%2Fnginx', full: true },
  { name: 'browse-docker-manifest', path: 'repositories/dockerhub-proxy/browse?image=library%2Fnginx&ref=latest', full: true },
  { name: 'browse-npm-versions', path: 'repositories/npm/browse?package=chalk', full: true },
  { name: 'browse-apt-packages', path: 'repositories/debian/browse?suite=trixie', full: true },
  { name: 'browse-raw', path: 'repositories/raw-releases/browse?path=jqlang%2Fjq%2Freleases%2Fdownload%2Fjq-1.8.1', full: true },
  { name: 'browse-all', path: 'browse', full: true },
  { name: 'repo-settings', path: 'repositories/dockerhub-proxy/settings', full: true },
]
const SHOTS = REAL ? REAL_SHOTS : MOCK_SHOTS

async function signIn(page) {
  await page.goto(BASE + 'sign-in', { waitUntil: 'networkidle' })
  await page.locator('input[autocomplete=username]').fill(ADMIN_USER)
  await page.locator('input[autocomplete=current-password]').fill(ADMIN_PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 15000 })
}

async function openSetup(page) {
  await page.getByRole('button', { name: /set me up/i }).first().click()
  await page.waitForTimeout(900)
}

async function createFlow(page) {
  await page.getByRole('button', { name: /create repository/i }).first().click()
  await page.waitForTimeout(500)
  await page.locator('.tile').first().click()
  await page.waitForTimeout(400)
  await page.locator('.tile--type').first().click()
  await page.waitForTimeout(300)
  await page.getByText('Google (gcr.io)', { exact: true }).first().click()
  await page.waitForTimeout(300)
}

mkdirSync(OUT, { recursive: true })
const browser = await chromium.launch()
for (const [device, viewport] of Object.entries(WIDTHS)) {
  for (const theme of THEMES) {
    for (const auth of [false, true]) {
      const context = await browser.newContext({ viewport, deviceScaleFactor: device === 'mobile' ? 2 : 1, colorScheme: theme })
      await context.addInitScript(
        ([t, a]) => {
          localStorage.setItem('ws.theme', t)
          if (a === 'mock') localStorage.setItem('ws.mock.session', 'admin')
          else localStorage.removeItem('ws.mock.session')
        },
        [theme, auth && !REAL ? 'mock' : ''],
      )
      const page = await context.newPage()
      page.on('pageerror', (e) => console.error(`[${device}/${theme}] page error: ${e.message}`))
      if (REAL && auth) await signIn(page)
      for (const shot of SHOTS.filter((s) => (s.auth ?? true) === auth)) {
        if (ONLY && !ONLY.test(shot.name)) continue
        await page.goto(BASE + shot.path, { waitUntil: 'networkidle' }).catch(() => undefined)
        await page.waitForTimeout(shot.settle ?? 1500)
        if (shot.action) await shot.action(page).catch((e) => console.error(`${shot.name}: ${e.message}`))
        const file = join(OUT, `${shot.name}-${theme}-${device}.png`)
        await page.screenshot({ path: file, fullPage: Boolean(shot.full) && (device === 'desktop' || Boolean(shot.fullMobile)) })
        console.log(file)
      }
      await context.close()
    }
  }
}
await browser.close()
