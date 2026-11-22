// SPDX-License-Identifier: GPL-3.0-or-later
// README screenshots: a handful of pages from the mock build at desktop size, written to Documents/Images/. Skipped
// unless README_SHOTS=1, so `make e2e` and CI never run it; `make readme-screenshots` does. The mock data uses public
// upstream names only; look at every image before committing it all the same.
import { test } from '@playwright/test'

import { open, prepare } from './support'

const OUT = process.env.README_SHOTS_DIR ?? '../../Documents/Images'
const SEED = Number(process.env.README_SHOTS_SEED ?? 3)

const SHOTS: { name: string; path: string; theme: 'light' | 'dark'; live?: boolean }[] = [
  { name: 'dashboard-light', path: '', theme: 'light', live: true },
  { name: 'dashboard-dark', path: '', theme: 'dark', live: true },
  { name: 'repositories', path: 'repositories', theme: 'light' },
  { name: 'docker-tags', path: 'repositories/dockerhub-proxy/packages?image=library%2Fnginx', theme: 'dark' },
  { name: 'npm-package', path: 'repositories/npm/packages?package=vue', theme: 'light' },
  { name: 'set-me-up', path: 'repositories/dockerhub-proxy?setup=1', theme: 'light' },
  { name: 'tasks', path: 'tasks', theme: 'dark' },
  { name: 'system', path: 'admin/system', theme: 'light' },
]

test.describe('README screenshots', () => {
  test.skip(process.env.README_SHOTS !== '1', 'set README_SHOTS=1 (make readme-screenshots)')
  test.use({ viewport: { width: 1440, height: 900 } })

  for (const shot of SHOTS) {
    test(shot.name, async ({ page }) => {
      await page.emulateMedia({ colorScheme: shot.theme })
      // Live shots keep the mock generator running so the dashboard shows transfers in flight; the others are still.
      await prepare(page, { still: !shot.live, load: shot.live ? 12 : 0 })
      if (shot.live) {
        // Seeded randomness for the live shots too, so a retake shows the same kind of moment.
        await page.addInitScript((seed) => {
          let s = seed
          Math.random = () => {
            s = (s + 0x6d2b79f5) | 0
            let t = s
            t = Math.imul(t ^ (t >>> 15), t | 1)
            t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296
          }
        }, SEED)
      }
      await open(page, shot.path)
      // The "Mock data" badge belongs to the demo build, not to the product.
      await page.addStyleTag({ content: '.topbar__mock { display: none !important; }' })
      if (shot.name === 'set-me-up') await page.getByRole('dialog').getByRole('button', { name: 'Linux' }).click()
      await page.waitForTimeout(shot.live ? 6000 : 800)
      await page.screenshot({ path: `${OUT}/${shot.name}.png`, fullPage: false, animations: 'disabled', caret: 'hide' })
    })
  }
})
