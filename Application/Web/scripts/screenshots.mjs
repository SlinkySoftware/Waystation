// SPDX-License-Identifier: GPL-3.0-or-later
// Screenshot tour of the mock UI (both themes, desktop + mobile) for review. Runs in the official Playwright
// image against a running `npm run dev:mock` (or a mock build), on the host network so the page is a secure
// context (http://localhost) and the MSW service worker can start:
//   docker run --rm --network host -v "$PWD:/src" -w /src/Application/Web mcr.microsoft.com/playwright:v1.65.0-noble \
//     sh -c 'npm i --no-save --prefix /tmp/pw playwright@1.65.0 >/dev/null && NODE_PATH=/tmp/pw/node_modules node scripts/screenshots.mjs'
// Env: BASE (default http://localhost:9173/ui/), OUT (default ../../Documents/Notes/screenshots/m0), ONLY (regex on shot name).
import { mkdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { join } from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = require('playwright')

const BASE = process.env.BASE ?? 'http://localhost:9173/ui/'
const OUT = process.env.OUT ?? '../../Documents/Notes/screenshots/m0'
const ONLY = process.env.ONLY ? new RegExp(process.env.ONLY) : null
const WIDTHS = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } }
const THEMES = ['light', 'dark']

/** name, path, signed in?, optional action before the shot, settle time (ms), full page? */
const SHOTS = [
  { name: 'sign-in', path: 'sign-in', auth: false },
  { name: 'dashboard', path: '', settle: 7000, full: true, fullMobile: true },
  { name: 'repositories', path: 'repositories', full: true },
  { name: 'create-repository', path: 'repositories', action: createFlow },
  { name: 'repo-docker-hub-setup', path: 'repositories/docker-hub', action: openSetup },
  { name: 'repo-npm-setup', path: 'repositories/npm-proxy', action: openSetup },
  { name: 'repo-debian-overview', path: 'repositories/debian', settle: 2500, full: true },
  { name: 'browse-docker-tags', path: 'repositories/docker-hub/browse?image=library%2Fnginx', full: true },
  { name: 'browse-docker-manifest', path: 'repositories/docker-hub/browse?image=library%2Fnginx&ref=1.29.3', full: true },
  { name: 'browse-npm-versions', path: 'repositories/npm-proxy/browse?package=typescript', full: true },
  { name: 'browse-apt-packages', path: 'repositories/debian/browse?suite=trixie', full: true },
  { name: 'browse-raw', path: 'repositories/raw-releases/browse?path=docker%2Fcompose%2Freleases%2Fdownload%2Fv2.40.3', full: true },
  { name: 'repo-settings', path: 'repositories/ghcr/settings', full: true },
]

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
          if (a) localStorage.setItem('ws.mock.session', 'admin')
          else localStorage.removeItem('ws.mock.session')
        },
        [theme, auth],
      )
      const page = await context.newPage()
      page.on('pageerror', (e) => console.error(`[${device}/${theme}] page error: ${e.message}`))
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
