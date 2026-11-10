// SPDX-License-Identifier: GPL-3.0-or-later
// The dashboard of users without stats.read (SPEC §4.10): the repositories they can use, or "no access yet".
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { seedRepositories } from '@/mocks/fixtures/repositories'
import { CAPS, signInAs } from '@/test/session'

import DashboardLanding from './DashboardLanding.vue'

const visible = seedRepositories().slice(0, 3)
const GET = vi.fn(async (path: string) => {
  if (path === '/repositories') return { items: visible, next_cursor: null, total: visible.length }
  throw new Error(`unexpected request ${path}`)
})

vi.mock('@/api/client', () => ({
  api: { GET: (path: string) => GET(path) },
  call: async <T,>(promise: Promise<T>) => promise,
}))

const page = { template: '<div />' }
const router = createRouter({
  history: createMemoryHistory(),
  routes: ['dashboard', 'profile', 'search', 'browse'].map((name) => ({ path: `/${name}`, name, component: page })).concat([{ path: '/repositories/:name', name: 'repository', component: page }]),
})

async function mountLanding() {
  const w = mount(DashboardLanding, { global: { plugins: [router] } })
  await flushPromises()
  return w
}

describe('dashboard landing', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    GET.mockClear()
  })

  it('tells a user without roles that there is no access yet, without asking the server for anything', async () => {
    signInAs(CAPS.noRoles)
    const w = await mountLanding()
    expect(w.text()).toContain('You are signed in, but have no access yet')
    expect(w.find('a[href="/profile"]').exists()).toBe(true)
    expect(GET).not.toHaveBeenCalled()
  })

  it('lists the repositories a reader can use, with links to them, browse and search', async () => {
    signInAs(CAPS.reader)
    const w = await mountLanding()
    expect(GET.mock.calls.map(([path]) => path)).toEqual(['/repositories'])
    for (const repo of visible) expect(w.find(`a[href="/repositories/${repo.name}"]`).exists()).toBe(true)
    expect(w.text()).toContain('3 repositories you can use')
    expect(w.find('a[href="/search"]').exists()).toBe(true)
  })

  it('says so when no repository is shared with a reader yet', async () => {
    signInAs(CAPS.reader)
    visible.splice(0)
    const w = await mountLanding()
    expect(w.text()).toContain('No repository is shared with you yet')
  })
})
