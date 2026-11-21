// SPDX-License-Identifier: GPL-3.0-or-later
// Top artifacts / recently added (dashboard) and the largest artifacts with "where used" (Storage page).
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { seedRepositories } from '@/mocks/fixtures/repositories'
import { digestUsage, topArtifacts } from '@/mocks/top'
import LargestArtifactsCard from '@/components/storage/LargestArtifactsCard.vue'

import TopArtifactsCard from './TopArtifactsCard.vue'

const repos = seedRepositories()
type Query = { by?: string; window?: string; limit?: number; repo?: string }
const GET = vi.fn(async (path: string, init?: { params?: { query?: Query; path?: { digest?: string } } }) => {
  const q = init?.params?.query ?? {}
  if (path === '/stats/top/artifacts') return topArtifacts(repos, (q.by ?? 'downloads') as 'downloads', (q.window ?? '7d') as '7d', q.repo ?? null, q.limit ?? 10)
  if (path === '/digests/{digest}/usage') return digestUsage(repos, init?.params?.path?.digest ?? '')
  throw new Error(`unexpected request ${path}`)
})

vi.mock('@/api/client', () => ({
  api: { GET: (path: string, init?: object) => GET(path, init as never) },
  call: async <T,>(promise: Promise<T>) => promise,
}))

const page = { template: '<div />' }
const router = createRouter({
  history: createMemoryHistory(),
  routes: [{ path: '/', name: 'dashboard', component: page }, { path: '/repositories/:name/:tab?', name: 'repository', component: page }],
})

describe('top artifacts card', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    GET.mockClear()
    localStorage.clear()
  })

  it('lists the most downloaded artifacts with links into the browse view, and switches to bytes', async () => {
    const w = mount(TopArtifactsCard, { props: { mode: 'top', limit: 5 }, global: { plugins: [router] } })
    await flushPromises()
    expect(GET).toHaveBeenLastCalledWith('/stats/top/artifacts', { params: { query: { by: 'downloads', window: '7d', limit: 5, repo: undefined } } })
    expect(w.findAll('li.top__row')).toHaveLength(5)
    expect(w.find('a.top__name').attributes('href')).toMatch(/^\/repositories\/[^/]+\/browse/)
    expect(w.text()).toContain('Most downloaded, last 7 days')
    await w.findAll('button').find((b) => b.text() === 'Bytes')!.trigger('click')
    await flushPromises()
    expect(GET).toHaveBeenLastCalledWith('/stats/top/artifacts', { params: { query: { by: 'bytes', window: '7d', limit: 5, repo: undefined } } })
    expect(localStorage.getItem('ws.dashboard.top.measure')).toBe('bytes')
  })

  it('shows recently added artifacts without the measure toggles', async () => {
    const w = mount(TopArtifactsCard, { props: { mode: 'recent', limit: 4 }, global: { plugins: [router] } })
    await flushPromises()
    expect(GET.mock.calls[0]![1]).toMatchObject({ params: { query: { by: 'recent' } } })
    expect(w.text()).toContain('Recently added')
    expect(w.findAll('button').some((b) => b.text() === 'Bytes')).toBe(false)
  })
})

describe('largest artifacts card', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    GET.mockClear()
  })

  it('asks for the largest artifacts and opens "where used" for one', async () => {
    const w = mount(LargestArtifactsCard, { props: { limit: 3 }, global: { plugins: [router] }, attachTo: document.body })
    await flushPromises()
    expect(GET.mock.calls[0]![1]).toMatchObject({ params: { query: { by: 'size', limit: 3 } } })
    expect(w.findAll('li.la__row')).toHaveLength(3)
    const largest = topArtifacts(repos, 'size', '7d', null, 3).items[0]!
    await w.find('li.la__row button').trigger('click')
    await flushPromises()
    expect(GET).toHaveBeenLastCalledWith('/digests/{digest}/usage', { params: { path: { digest: largest.sha256 } } })
    expect(document.body.textContent).toContain(`Where is ${largest.display} used?`)
    w.unmount()
  })
})
