// SPDX-License-Identifier: GPL-3.0-or-later
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { StorageGuard } from '@/api/types'

import LowDiskBanner from './LowDiskBanner.vue'

const GiB = 1024 ** 3
const state: { guard: StorageGuard } = {
  guard: { min_free_bytes: 50 * GiB, min_free_percent: 0, floor_bytes: 50 * GiB, free_bytes: 900 * GiB, total_bytes: 2048 * GiB, low: false, low_since: null, updated_at: null },
}

vi.mock('@/api/client', () => ({
  api: { GET: vi.fn(async () => structuredClone(state.guard)) },
  call: async <T,>(promise: Promise<T>) => promise,
}))

const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/storage', name: 'storage', component: { template: '<div />' } },
  ] })

async function mountBanner() {
  const w = mount(LowDiskBanner, { global: { plugins: [router] } })
  await flushPromises()
  return w
}

describe('low-disk banner', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    window.sessionStorage.clear()
    state.guard = { ...state.guard, low: false, low_since: null, free_bytes: 900 * GiB }
  })

  it('stays hidden while there is enough free space', async () => {
    const w = await mountBanner()
    expect(w.find('[data-test="low-disk-banner"]').exists()).toBe(false)
  })

  it('shows free space and the guard while writes are refused, links to Storage, and can be dismissed', async () => {
    state.guard = { ...state.guard, low: true, low_since: '2026-11-10T01:00:00Z', free_bytes: 20 * GiB }
    const w = await mountBanner()
    const banner = w.find('[data-test="low-disk-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.attributes('role')).toBe('alert')
    expect(banner.text()).toContain('20.0 GiB free, below the 50.0 GiB guard')
    expect(banner.text()).toContain('cached content is still served')
    await w.find('button[aria-label="Dismiss"]').trigger('click')
    expect(w.find('[data-test="low-disk-banner"]').exists()).toBe(false)

    // Dismissed for the session, per episode: a new low-disk episode shows it again.
    const again = await mountBanner()
    expect(again.find('[data-test="low-disk-banner"]').exists()).toBe(false)
    state.guard = { ...state.guard, low_since: '2026-11-10T05:00:00Z' }
    const next = await mountBanner()
    expect(next.find('[data-test="low-disk-banner"]').exists()).toBe(true)
  })
})
