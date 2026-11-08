// SPDX-License-Identifier: GPL-3.0-or-later
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { QLayout, QPageContainer } from 'quasar'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

import { seedRepositories } from '@/mocks/fixtures/repositories'
import { mockSearch } from '@/mocks/search'

import SearchPage from './SearchPage.vue'

const repos = seedRepositories()
const requests: Record<string, unknown>[] = []

vi.mock('@/api/client', () => ({
  api: {
    GET: async (path: string, init?: { params?: { query?: Record<string, unknown> } }) => {
      const q = init?.params?.query ?? {}
      if (path === '/repositories') return { items: repos, next_cursor: null }
      requests.push(q)
      const list = (v: unknown) => (Array.isArray(v) ? v.map(String) : [])
      const offset = q.cursor ? Number(atob(String(q.cursor))) : 0
      return mockSearch(
        repos,
        { q: String(q.q ?? ''), formats: list(q.format), repositories: list(q.repository), kinds: list(q.kind), types: list(q.type), cached: q.cached === true },
        offset,
        Number(q.limit ?? 50),
        true,
      )
    },
  },
  call: async <T,>(promise: Promise<T>) => promise,
  errorMessage: (e: unknown) => String(e),
}))

const blank = { template: '<div />' }

async function setup(url: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/search', name: 'search', component: SearchPage },
      { path: '/repositories/:name/:tab?', name: 'repository', component: blank },
    ],
  })
  await router.push(url)
  const Shell = defineComponent({ render: () => h(QLayout, () => h(QPageContainer, () => h(SearchPage))) })
  const wrapper = mount(Shell, { attachTo: document.body, global: { plugins: [router] } })
  await flushPromises()
  return { router, wrapper }
}

describe('SearchPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    requests.length = 0
  })
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('starts with the search illustration and examples, without listing anything', async () => {
    const { wrapper } = await setup('/search')
    expect(wrapper.text()).toContain('Search every repository')
    expect(wrapper.findAll('a.result')).toHaveLength(0)
    expect(requests.at(-1)).toMatchObject({ limit: 1 })
  })

  it('shows results with facets from the URL and links them to Browse', async () => {
    const { wrapper } = await setup('/search?q=nginx')
    const rows = wrapper.findAll('a.result')
    expect(rows.length).toBeGreaterThan(0)
    expect(wrapper.text()).toMatch(/\d+ results?/)
    const image = rows.find((r) => r.text().includes('library/nginx'))!
    const href = new URL(image.attributes('href')!, 'https://registry.example.com')
    expect(href.pathname).toBe('/repositories/dockerhub-proxy/browse')
    expect(href.searchParams.get('image')).toBe('library/nginx')
    expect(href.searchParams.get('ref')).toBeTruthy()
    expect(image.find('mark').text()).toBe('nginx')
    expect(wrapper.find('nav[aria-label="Filters"]').exists()).toBe(true)
  })

  it('keeps facet selections in the URL and sends them to the API', async () => {
    const { router, wrapper } = await setup('/search?q=nginx')
    const apt = wrapper.findAll('.facet__value').find((l) => l.text().startsWith('apt'))!
    await apt.find('input').trigger('change')
    await flushPromises()
    expect(router.currentRoute.value.query.format).toEqual(['apt'])
    expect(requests.at(-1)).toMatchObject({ q: 'nginx', format: ['apt'] })
    const aptRepos = new Set(repos.filter((r) => r.format === 'apt').map((r) => r.name))
    const shown = wrapper.findAll('a.result').map((r) => r.attributes('href')!.split('/')[2]!)
    expect(shown.length).toBeGreaterThan(0)
    expect(shown.every((name) => aptRepos.has(name))).toBe(true)
  })

  it('says so when nothing matches, with a way out of the filters', async () => {
    const { wrapper } = await setup('/search?q=zzzz-nothing&kind=image')
    expect(wrapper.text()).toContain('No matches for “zzzz-nothing”')
    expect(wrapper.text()).toContain('Clear filters')
  })

  it('moves through the results with the arrow keys and back to the box with Escape', async () => {
    const { wrapper } = await setup('/search?q=nginx')
    const input = wrapper.find('.search__box input')
    const rows = wrapper.findAll('a.result')
    await input.trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(rows[0]!.element)
    await wrapper.find('ul').trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(rows[1]!.element)
    await wrapper.find('ul').trigger('keydown', { key: 'Escape' })
    expect(document.activeElement).toBe(input.element)
  })
})
