// SPDX-License-Identifier: GPL-3.0-or-later
// The rail and the routes for minimal-permission sessions (SPEC §4.10 "What a user without roles sees").
import { describe, expect, it } from 'vitest'
import type { RouteRecordRaw } from 'vue-router'

import routes from '@/router/routes'
import { CAPS } from '@/test/session'

import { navigation, visibleNavigation } from './navigation'

const can = (caps: string[]) => (capability: string) => caps.includes('admin') || caps.includes(capability)
const rail = (caps: string[]) => visibleNavigation(can(caps)).flatMap((g) => g.items.map((i) => i.name))

function flatten(records: RouteRecordRaw[]): RouteRecordRaw[] {
  return records.flatMap((r) => [r, ...flatten(r.children ?? [])])
}
const named = new Map(flatten(routes).filter((r) => typeof r.name === 'string').map((r) => [r.name as string, r]))

describe('navigation by capability', () => {
  it('shows a user without roles only the dashboard (the "no access yet" landing)', () => {
    expect(rail(CAPS.noRoles)).toEqual(['dashboard'])
  })

  it('shows a reader (or the anonymous-access grants) the repository pages, no statistics', () => {
    expect(rail(CAPS.reader)).toEqual(['dashboard', 'repositories', 'browse', 'search'])
  })

  it('adds activity and storage for stats.read, and everything for an administrator', () => {
    expect(rail(CAPS.viewer)).toEqual(['dashboard', 'repositories', 'browse', 'search', 'activity', 'storage'])
    expect(rail(CAPS.admin)).toEqual(navigation.flatMap((g) => g.items.map((i) => i.name)))
  })

  it('drops the Administration heading when none of its items is allowed', () => {
    expect(visibleNavigation(can(CAPS.viewer)).map((g) => g.label)).toEqual([undefined])
  })

  it('guards every rail page with the capability that shows it', () => {
    for (const item of navigation.flatMap((g) => g.items)) {
      expect(named.get(item.name)?.meta?.capability, item.name).toBe(item.capability)
    }
  })

  it('needs a capability for every page except the dashboard, profile and the fallbacks', () => {
    const open = [...named.values()].filter((r) => !r.meta?.public && !r.meta?.capability && r.component).map((r) => r.name)
    expect(open.sort()).toEqual(['dashboard', 'no-access', 'not-found', 'profile'])
  })
})
