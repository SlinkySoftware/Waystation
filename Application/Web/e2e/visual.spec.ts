// SPDX-License-Identifier: GPL-3.0-or-later
// Visual-regression baseline (UI_DESIGN §7, T-U09): key pages in both themes at three widths, on the mock build in
// still mode (no live generator, seeded data, fixed clock). Baselines live in e2e/__screenshots__; after an intended
// change run `make e2e-update` and review the new images in the diff.
import { expect, test } from '@playwright/test'

import { open, prepare } from './support'

const WIDTHS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'tablet', width: 1024, height: 768 },
  { name: 'phone', width: 390, height: 844 },
] as const

const SHOTS: { name: string; path: string; user?: string | null }[] = [
  { name: 'sign-in', path: 'sign-in', user: null },
  { name: 'dashboard', path: '' },
  { name: 'repositories', path: 'repositories' },
  { name: 'repository', path: 'repositories/dockerhub-proxy' },
  { name: 'docker-tags', path: 'repositories/dockerhub-proxy/packages?image=library%2Fnginx' },
  { name: 'tree', path: 'repositories/raw-releases/browse?path=cli%2Fcli%2Freleases%2Fdownload%2Fv2.83.0%2Fgh_2.83.0_linux_amd64.tar.gz' },
  { name: 'activity', path: 'activity' },
  { name: 'storage', path: 'storage' },
  { name: 'profile', path: 'profile' },
]

for (const theme of ['light', 'dark'] as const) {
  for (const size of WIDTHS) {
    test.describe(`${theme} ${size.name}`, () => {
      test.use({ colorScheme: theme, viewport: { width: size.width, height: size.height } })
      for (const shot of SHOTS) {
        test(shot.name, async ({ page }) => {
          await prepare(page, { still: true, user: shot.user === undefined ? 'admin' : shot.user })
          await open(page, shot.path)
          await page.waitForTimeout(400)
          // The live-transfer panel is masked: its rows depend on when the event stream attaches, so it differs
          // between runs even in still mode. Its behaviour and frame rate are covered by flows.spec and perf.spec.
          await expect(page).toHaveScreenshot(`${shot.name}-${theme}-${size.name}.png`, {
            fullPage: false,
            mask: [page.locator('.live-xfers')],
          })
        })
      }
    })
  }
}
