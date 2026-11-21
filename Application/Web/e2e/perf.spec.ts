// SPDX-License-Identifier: GPL-3.0-or-later
// UI performance checks on the mock build (T-U03, T-U05; ARCHITECTURE §9): the dashboard with 200 live transfers
// and the browse tree over a folder of 100,000 files. Frame times come from requestAnimationFrame in the page; the
// results are written to test-results/ui-perf.json (and printed). Limits: 60 fps means a median frame within
// 1000/60 ms; the rest guard against jank (p95, long frames) with headroom for software rendering in CI.
import { writeFileSync } from 'node:fs'

import { expect, type Page, test } from '@playwright/test'

import { open, prepare } from './support'

interface Frames {
  frames: number
  fps: number
  median: number
  p95: number
  long: number
}

// Measured alone (the `perf` project runs after the others, one test at a time): frame rates mean nothing while
// other browsers share the CPU.
test.describe.configure({ mode: 'serial' })

const results: Record<string, unknown> = {}
test.afterAll(() => {
  writeFileSync('test-results/ui-perf.json', JSON.stringify(results, null, 2))
  console.log(JSON.stringify(results, null, 2))
})

/** Record frame intervals for `ms` while `work` runs (if any). */
async function measure(page: Page, ms: number, work?: () => Promise<void>): Promise<Frames> {
  await page.evaluate((duration) => {
    const w = window as unknown as { __frames: number[] }
    w.__frames = []
    let last = performance.now()
    const end = last + duration
    const tick = (now: number) => {
      w.__frames.push(now - last)
      last = now
      if (now < end) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  }, ms)
  if (work) await work()
  await page.waitForTimeout(ms + 200)
  const gaps = (await page.evaluate(() => (window as unknown as { __frames: number[] }).__frames)).slice(1).sort((a, b) => a - b)
  const pick = (q: number) => gaps[Math.min(gaps.length - 1, Math.floor(gaps.length * q))] ?? Infinity
  const total = gaps.reduce((a, b) => a + b, 0)
  return { frames: gaps.length, fps: Math.round((gaps.length / total) * 1000 * 10) / 10, median: Math.round(pick(0.5) * 10) / 10, p95: Math.round(pick(0.95) * 10) / 10, long: gaps.filter((g) => g > 50).length }
}

test('dashboard with 200 live transfers stays at 60 fps', async ({ page }) => {
  await prepare(page, { load: 200 })
  await open(page, '')
  await expect(async () => {
    const count = await page.locator('button.live .live__count').innerText()
    expect(Number(count)).toBeGreaterThanOrEqual(180)
  }).toPass({ timeout: 60_000 })
  const frames = await measure(page, 5000)
  results.dashboard200 = frames
  expect(frames.median).toBeLessThanOrEqual(17.5)
  expect(frames.p95).toBeLessThanOrEqual(50)
})

test('browse tree over 100,000 files scrolls smoothly', async ({ page }) => {
  await prepare(page, { still: true })
  const started = Date.now()
  await open(page, 'repositories/raw-releases/browse?path=mirror%2Fnightly')
  await expect(page.getByRole('treeitem', { name: /build-000001/ })).toBeVisible()
  const firstPage = Date.now() - started
  const tree = page.getByRole('tree')
  // Load pages until 20,000 rows are in the tree (40 requests), then scroll through them.
  await expect(async () => {
    await tree.evaluate((el) => (el.scrollTop = el.scrollHeight))
    const rows = await tree.evaluate((el) => (el.firstElementChild as HTMLElement).offsetHeight / 32)
    expect(rows).toBeGreaterThan(20_000)
  }).toPass({ timeout: 60_000, intervals: [100] })
  const frames = await measure(page, 3000, async () => {
    await tree.evaluate((el) => {
      const step = () => {
        el.scrollTop = Math.max(0, el.scrollTop - 480) // 15 rows a frame: a fast flick
        if (el.scrollTop > 0) requestAnimationFrame(step)
      }
      requestAnimationFrame(step)
    })
  })
  const domRows = await page.getByRole('treeitem').count()
  results.tree100k = { firstPageMs: firstPage, domRows, ...frames }
  expect(domRows).toBeLessThan(120) // virtualised: the DOM holds a window, not the folder
  expect(frames.median).toBeLessThanOrEqual(17.5)
  expect(frames.p95).toBeLessThanOrEqual(50)
  // End jumps to the last loaded row (the next page then loads as it shows) and keeps it in view.
  await tree.focus()
  await page.keyboard.press('End')
  const active = await tree.getAttribute('aria-activedescendant')
  expect(active).toBeTruthy()
  await expect(page.locator(`[id="${active}"]`)).toBeVisible()
})
