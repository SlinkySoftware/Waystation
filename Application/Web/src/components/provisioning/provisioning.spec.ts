// SPDX-License-Identifier: GPL-3.0-or-later
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { ProvisioningItem, ProvisioningPlan } from '@/api/types'
import ManagedBadge from '@/components/ManagedBadge.vue'
import { isManaged } from '@/utils/managed'

import { changeCount, describeChange, formatValue, visibleItems } from './provisioning'

const items: ProvisioningItem[] = [
  { kind: 'role', name: 'docker-pull', action: 'create', changes: [] },
  { kind: 'repository', name: 'npm', action: 'unchanged', changes: [] },
  { kind: 'webhook', name: 'chat', action: 'prune', changes: [] },
]

describe('provisioning page helpers', () => {
  it('hides unchanged items unless asked', () => {
    expect(visibleItems(items, false).map((i) => i.name)).toEqual(['docker-pull', 'chat'])
    expect(visibleItems(items, true)).toHaveLength(3)
  })

  it('counts the changes an apply would make', () => {
    const plan: ProvisioningPlan = { file: '/f', sha256: null, valid: true, summary: { create: 2, update: 1, unchanged: 9, prune: 1, failed: 0 } }
    expect(changeCount(plan)).toBe(4)
    expect(changeCount(null)).toBe(0)
  })

  it('prints changes, secrets only as changed', () => {
    expect(describeChange({ field: 'description', before: 'a', after: 'b' })).toBe('description: a → b')
    expect(describeChange({ field: 'enabled', before: true, after: false })).toBe('enabled: true → false')
    expect(describeChange({ field: 'proxy.auth.secret', before: '***', after: '***', redacted: true })).toBe('proxy.auth.secret: secret changed')
    expect(formatValue(null)).toBe('not set')
    expect(formatValue(['a', 'b'])).toBe('["a","b"]')
    expect(formatValue('x'.repeat(400)).length).toBeLessThanOrEqual(160)
  })
})

describe('ManagedBadge (T-O09 UI label)', () => {
  it('labels objects the provisioning file declares, with the hint', () => {
    const w = mount(ManagedBadge, { props: { item: { managed_by: 'provisioning' } } })
    expect(w.text()).toContain('Managed')
    expect(w.attributes('aria-label')).toContain('provisioning file')
    expect(isManaged({ managed_by: 'provisioning' })).toBe(true)
  })

  it('renders nothing for other objects', () => {
    expect(mount(ManagedBadge, { props: { item: { managed_by: null } } }).html()).not.toContain('Managed')
    expect(mount(ManagedBadge, { props: { item: null } }).find('.managed-badge').exists()).toBe(false)
    expect(isManaged(undefined)).toBe(false)
  })
})
