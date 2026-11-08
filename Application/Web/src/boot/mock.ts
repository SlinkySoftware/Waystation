// SPDX-License-Identifier: GPL-3.0-or-later
// In mock builds, start MSW before anything calls the API. In normal builds the condition is a constant false and
// the mocks (MSW, fixtures, generator) are tree-shaken out entirely.
import { defineBoot } from '#q-app'

export default defineBoot(async () => {
  if (import.meta.env.WAYSTATION_MOCK === '1') {
    const { startMocks } = await import('@/mocks')
    await startMocks()
  }
})
