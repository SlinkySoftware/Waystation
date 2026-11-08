// SPDX-License-Identifier: GPL-3.0-or-later
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { ApiToken } from '@/api/types'
import { randomSecret } from '@/mocks/tokens'

import TokenScopes from './TokenScopes.vue'
import TokenStatus from './TokenStatus.vue'
import { dateModel, expiresAt, isFutureDate, scopeLabel } from './tokens'

const NOW = new Date(2026, 10, 8, 12, 0, 0)

const token: ApiToken = {
  id: 't1',
  name: 'CI',
  prefix: 'wst_abcdefgh',
  owner: { kind: 'user', id: 'u1', name: 'admin' },
  scopes: ['read', 'admin'],
  repositories: [],
  status: 'active',
  expires_at: null,
  created_at: NOW.toISOString(),
  last_used_at: null,
  last_used_ip: null,
  revoked_at: null,
}

describe('token helpers', () => {
  it('turns expiry choices into timestamps', () => {
    expect(expiresAt('never', null, NOW)).toBeNull()
    expect(Date.parse(expiresAt('30', null, NOW)!) - NOW.getTime()).toBe(30 * 86_400_000)
    const custom = new Date(expiresAt('custom', '2026/12/24', NOW)!)
    expect([custom.getFullYear(), custom.getMonth(), custom.getDate(), custom.getHours()]).toEqual([2026, 11, 24, 23])
    expect(expiresAt('custom', null, NOW)).toBeNull()
  })

  it('accepts only future custom dates', () => {
    expect(dateModel(0, NOW)).toBe('2026/11/08')
    expect(isFutureDate('2026/11/09', NOW)).toBe(true)
    expect(isFutureDate('2026/11/08', NOW)).toBe(false)
    expect(isFutureDate('2025/12/31', NOW)).toBe(false)
  })

  it('labels scopes', () => {
    expect(scopeLabel('stats-read')).toBe('Stats')
  })

  it('mock secrets look like server tokens', () => {
    expect(randomSecret()).toMatch(/^wst_[A-Za-z0-9_-]{43}$/)
  })
})

describe('token components', () => {
  it('TokenStatus pairs colour with an icon and label', () => {
    const w = mount(TokenStatus, { props: { status: 'revoked' } })
    expect(w.text()).toBe('Revoked')
    expect(w.classes()).toContain('tone-blocked')
    expect(w.find('.q-icon').exists()).toBe(true)
  })

  it('TokenScopes lists scopes and repository limits', () => {
    expect(mount(TokenScopes, { props: { token } }).text()).toContain('All repositories')
    const limited = mount(TokenScopes, { props: { token: { ...token, repositories: ['docker-*', 'format:npm'] } } })
    expect(limited.text()).toContain('docker-*')
    expect(limited.text()).toContain('format:npm')
    expect(limited.text()).not.toContain('All repositories')
    expect(limited.findAll('.token-scopes__chip').map((c) => c.text())).toEqual(['Read', 'Admin'])
  })
})
