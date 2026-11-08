// SPDX-License-Identifier: GPL-3.0-or-later
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { Transfer } from '@/api/types'

import CacheBadge from './CacheBadge.vue'
import TransferRow from './live/TransferRow.vue'
import SizeCell from './SizeCell.vue'
import StatusPill from './StatusPill.vue'

const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/repositories/:name/:tab?', name: 'repository', component: { template: '<div />' } }] })

describe('base components', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('CacheBadge shows the state label with an icon and the follower count', () => {
    const w = mount(CacheBadge, { props: { state: 'FETCHING', followers: 3 } })
    expect(w.text()).toContain('FETCHING')
    expect(w.text()).toContain('+3')
    expect(w.find('.q-icon').exists()).toBe(true)
    expect(w.classes()).toContain('tone-miss')
  })

  it('StatusPill pairs colour with an icon and label', () => {
    const w = mount(StatusPill, { props: { status: 'blocked', tooltip: false } })
    expect(w.text()).toBe('Blocked')
    expect(w.classes()).toContain('tone-blocked')
    expect(w.find('.q-icon').exists()).toBe(true)
  })

  it('SizeCell uses binary units', () => {
    expect(mount(SizeCell, { props: { bytes: 1536 } }).text()).toBe('1.50 KiB')
    expect(mount(SizeCell, { props: { bytes: null } }).text()).toBe('–')
  })

  it('TransferRow renders progress, speed and ETA, and never renders the artifact as HTML', () => {
    const transfer: Transfer & { endedAt: null } = {
      id: 't1',
      repository: 'npm-proxy',
      format: 'npm',
      direction: 'download',
      artifact: '<img src=x onerror=alert(1)>.tgz',
      client: { ip: '192.0.2.7', user: 'alice' },
      cache: 'HIT',
      followers: 0,
      bytes_done: 512 * 1024,
      bytes_total: 1024 * 1024,
      rate_bps: 2_000_000,
      started_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      state: 'active',
      endedAt: null,
    }
    const w = mount(TransferRow, { props: { transfer }, global: { plugins: [router] } })
    expect(w.text()).toContain('50%')
    expect(w.text()).toContain('2.00 MB/s')
    expect(w.text()).toContain('alice')
    expect(w.find('img').exists()).toBe(false)
    expect(w.find('[role="progressbar"]').attributes('aria-valuenow')).toBe('50')
  })
})
