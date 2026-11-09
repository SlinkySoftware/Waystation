// SPDX-License-Identifier: GPL-3.0-or-later
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { RepositoryQuota } from '@/api/types'
import QuotaMeter from '@/components/repositories/QuotaMeter.vue'

import { fromBytes, quotaMeta, toBytes } from './quota'

const GiB = 1024 ** 3
const quota = (used: number, state: RepositoryQuota['state'], mode: RepositoryQuota['mode'] = 'soft'): RepositoryQuota => ({
  quota_bytes: 10 * GiB,
  mode,
  used_bytes: used * GiB,
  used_ratio: used / 10,
  state,
})

describe('quota display', () => {
  it('maps each state to a tone, an icon and a label (never colour alone)', () => {
    expect(quotaMeta(quota(2, 'ok'))).toMatchObject({ tone: 'tone-ok', label: 'Within quota', percent: '20%', usage: '2.00 GiB of 10.0 GiB' })
    expect(quotaMeta(quota(8.5, 'warning'))).toMatchObject({ tone: 'tone-degraded', label: 'Near quota' })
    const over = quotaMeta(quota(12, 'exceeded', 'hard'))
    expect(over).toMatchObject({ tone: 'tone-blocked', label: 'Over quota', fill: 1 })
    expect(over.description).toContain('refused')
    expect(quotaMeta(quota(12, 'exceeded', 'soft')).description).toContain('nothing is refused')
  })

  it('converts form sizes to bytes and back', () => {
    expect(toBytes(1.5, 'GiB')).toBe(1.5 * GiB)
    expect(toBytes(0, 'MiB')).toBe(1)
    expect(fromBytes(512 * 1024 ** 2)).toEqual({ size: 512, unit: 'MiB' })
    expect(fromBytes(50 * GiB)).toEqual({ size: 50, unit: 'GiB' })
    expect(fromBytes(3 * 1024 ** 4)).toEqual({ size: 3, unit: 'TiB' })
    expect(fromBytes(1000)).toEqual({ size: 0, unit: 'MiB' })
  })

  it('QuotaMeter shows usage, a meter and a badge from the warning level', () => {
    const ok = mount(QuotaMeter, { props: { quota: quota(2, 'ok'), compact: true } })
    expect(ok.find('[data-test="quota-badge"]').exists()).toBe(false)
    expect(ok.find('[role="meter"]').attributes('aria-valuetext')).toBe('2.00 GiB of 10.0 GiB')
    const warn = mount(QuotaMeter, { props: { quota: quota(8.5, 'warning'), compact: true } })
    expect(warn.find('[data-test="quota-badge"]').text()).toContain('Near quota')
    expect(warn.classes()).toContain('quota--warning')
    const full = mount(QuotaMeter, { props: { quota: quota(12, 'exceeded', 'hard') } })
    expect(full.text()).toContain('Hard')
    expect(full.find('.quota__fill').attributes('style')).toContain('width: 100%')
  })
})
