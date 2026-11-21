// SPDX-License-Identifier: GPL-3.0-or-later
// Accessibility (UI_DESIGN §6, T-U09): axe-core with the WCAG 2.0/2.1/2.2 A and AA rules on every page an
// administrator can open, in both themes, at desktop and phone widths. Zero serious or critical violations.
import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

import { open, PAGES, prepare } from './support'

const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

const VARIANTS = [
  { name: 'light', colorScheme: 'light', viewport: { width: 1440, height: 900 } },
  { name: 'dark', colorScheme: 'dark', viewport: { width: 1440, height: 900 } },
  { name: 'phone', colorScheme: 'light', viewport: { width: 390, height: 844 } },
] as const

for (const variant of VARIANTS) {
  test.describe(`axe ${variant.name}`, () => {
    test.use({ colorScheme: variant.colorScheme, viewport: variant.viewport })
    for (const target of [...PAGES, { name: 'sign-in', path: 'sign-in' }]) {
      test(target.name, async ({ page }) => {
        await prepare(page, { still: true, user: target.name === 'sign-in' ? null : 'admin' })
        await open(page, target.path)
        await page.waitForTimeout(300)
        const results = await new AxeBuilder({ page }).withTags(TAGS).analyze()
        const serious = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')
        const report = serious.map((v) => `${v.impact} ${v.id}: ${v.help}\n${v.nodes.slice(0, 5).map((n) => `    ${n.target.join(' ')}${n.failureSummary ? `\n      ${n.failureSummary.replace(/\n/g, '\n      ')}` : ''}`).join('\n')}`)
        expect(report, `${target.path} in ${variant.name}`).toEqual([])
      })
    }
  })
}
