// SPDX-License-Identifier: GPL-3.0-or-later
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { seedRepositories } from '@/mocks/fixtures/repositories'
import { mockSearch } from '@/mocks/search'
import { useRecentStore } from '@/stores/recent'

import CommandPalette from './CommandPalette.vue'

const repos = seedRepositories()

vi.mock('@/api/client', () => ({
  api: {
    GET: async (path: string, init?: { params?: { query?: Record<string, unknown> } }) => {
      const query = init?.params?.query ?? {}
      if (path === '/repositories') return { items: repos, next_cursor: null }
      return mockSearch(repos, { q: String(query.q ?? ''), formats: [], repositories: [], kinds: [], types: [], cached: false }, 0, Number(query.limit ?? 6), false)
    },
  },
  call: async <T,>(promise: Promise<T>) => promise,
  errorMessage: (e: unknown) => String(e),
}))

const page = { template: '<div />' }

function setup() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'dashboard', component: page },
      { path: '/search', name: 'search', component: page },
      { path: '/repositories', name: 'repositories', component: page },
      { path: '/repositories/:name/:tab?', name: 'repository', component: page },
      { path: '/:rest(.*)*', component: page },
    ],
  })
  const wrapper = mount(CommandPalette, { props: { modelValue: true }, attachTo: document.body, global: { plugins: [router] } })
  const input = () => document.querySelector<HTMLInputElement>('input[role="combobox"]')!
  const options = () => Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
  const active = () => document.getElementById(input().getAttribute('aria-activedescendant') ?? '')
  const type = async (text: string) => {
    input().value = text
    input().dispatchEvent(new Event('input'))
    await flushPromises()
  }
  const key = async (k: string) => {
    input().dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }))
    await flushPromises()
  }
  return { router, wrapper, input, options, active, type, key }
}

describe('CommandPalette', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })
  afterEach(() => {
    vi.useRealTimers()
    document.body.innerHTML = ''
  })

  it('is an ARIA combobox driving a listbox, focused on open', async () => {
    const { input, options, active } = setup()
    await flushPromises()
    expect(document.querySelector('[role="dialog"]')?.getAttribute('aria-modal')).toBe('true')
    expect(document.activeElement).toBe(input())
    const listbox = document.getElementById(input().getAttribute('aria-controls')!)
    expect(listbox?.getAttribute('role')).toBe('listbox')
    expect(options().length).toBeGreaterThan(3)
    expect(active()?.getAttribute('aria-selected')).toBe('true')
    expect(active()).toBe(options()[0])
  })

  it('filters repositories and pages as you type and opens the choice with the keyboard', async () => {
    const { router, wrapper, options, active, type, key } = setup()
    await flushPromises()
    await type('deb')
    const labels = options().map((o) => o.querySelector('.palette__label')?.textContent)
    expect(labels[0]).toBe('Search for “deb”')
    expect(labels).toContain('debian')
    expect(labels).toContain('debian-security')
    expect(labels).not.toContain('npm')
    await key('ArrowDown')
    expect(active()?.textContent).toContain('debian')
    await key('ArrowUp')
    await key('ArrowUp')
    expect(active()).toBe(options()[options().length - 1])
    await key('ArrowDown')
    await key('ArrowDown')
    await key('Enter')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('repository')
    expect(router.currentRoute.value.params.name).toBe('debian')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
    expect(useRecentStore().items[0]).toMatchObject({ id: 'repo:debian', kind: 'repository' })
  })

  it('runs a quick search and opens all results for the query', async () => {
    vi.useFakeTimers()
    const { router, options, type, key } = setup()
    await flushPromises()
    await type('nginx')
    await vi.advanceTimersByTimeAsync(200)
    await flushPromises()
    const results = options().filter((o) => o.closest('[role="group"]')?.textContent?.includes('Packages, images and files'))
    expect(results.length).toBeGreaterThan(0)
    expect(results.length).toBeLessThanOrEqual(6)
    await key('Enter')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('search')
    expect(router.currentRoute.value.query.q).toBe('nginx')
    expect(useRecentStore().items[0]).toMatchObject({ kind: 'query', label: 'nginx' })
  })

  it('closes on Escape', async () => {
    const { wrapper, key } = setup()
    await flushPromises()
    await key('Escape')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('closes on a click outside the dialog', async () => {
    const { wrapper } = setup()
    await flushPromises()
    document.querySelector('.palette-backdrop')!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })
})
