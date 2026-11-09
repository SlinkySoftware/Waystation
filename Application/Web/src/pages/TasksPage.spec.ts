// SPDX-License-Identifier: GPL-3.0-or-later
// The Tasks page against the mock API (MSW handlers answered in-process) and the mock live source.
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { QLayout, QPageContainer } from 'quasar'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { Session } from '@/api/types'
import { WorldLiveSource } from '@/mocks/liveSource'
import { useAuthStore } from '@/stores/auth'
import { setLiveSourceFactory } from '@/stores/liveSource'

import TasksPage from './TasksPage.vue'

vi.mock('@/api/client', async () => {
  const { getResponse } = await import('msw')
  const { handlers } = await import('@/mocks/handlers')
  class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly body: { errors?: { field: string; message: string }[] },
    ) {
      super(`HTTP ${status}`)
    }
    get fieldErrors(): Record<string, string> {
      return Object.fromEntries((this.body.errors ?? []).map((e) => [e.field, e.message]))
    }
  }
  async function send(method: string, path: string, init?: { params?: { path?: Record<string, string>; query?: Record<string, unknown> }; body?: unknown }) {
    let url = path
    for (const [k, v] of Object.entries(init?.params?.path ?? {})) url = url.replace(`{${k}}`, encodeURIComponent(v))
    const query = new URLSearchParams()
    for (const [k, v] of Object.entries(init?.params?.query ?? {})) {
      if (v === undefined) continue
      for (const item of Array.isArray(v) ? v : [v]) query.append(k, String(item))
    }
    const request = new Request(new URL(`/api/v1${url}${query.size ? `?${query.toString()}` : ''}`, location.origin), {
      method,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'mock-csrf-7f3a9c' },
      body: init?.body === undefined ? undefined : JSON.stringify(init.body),
    })
    const response = (await getResponse(handlers, request))!
    const body: unknown = response.status === 204 ? null : await response.json()
    if (!response.ok) throw new ApiError(response.status, body as { errors?: { field: string; message: string }[] })
    return body
  }
  return {
    api: {
      GET: (path: string, init?: never) => send('GET', path, init),
      POST: (path: string, init?: never) => send('POST', path, init),
      PATCH: (path: string, init?: never) => send('PATCH', path, init),
      DELETE: (path: string, init?: never) => send('DELETE', path, init),
    },
    ApiError,
    call: async <T,>(promise: Promise<T>) => promise,
    errorMessage: (e: unknown) => String(e),
  }
})

function session(capabilities: string[]): Session {
  return {
    user: { id: 'u1', username: 'admin', display_name: 'Administrator', email: null, source: 'local' },
    csrf_token: 'mock-csrf-7f3a9c',
    capabilities,
    preferences: { theme: 'system', density: 'comfortable' },
    expires_at: '2099-01-01T00:00:00Z',
  } as Session
}

async function setup(url: string, capabilities = ['admin']) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/tasks', name: 'tasks', component: TasksPage },
      { path: '/cleanup', name: 'cleanup', component: { template: '<div />' } },
    ],
  })
  useAuthStore().session = session(capabilities)
  await router.push(url)
  const Shell = defineComponent({ render: () => h(QLayout, () => h(QPageContainer, () => h(TasksPage))) })
  const wrapper = mount(Shell, { attachTo: document.body, global: { plugins: [router] } })
  await flushPromises()
  await vi.waitFor(() => expect(document.body.textContent).toContain('Workers'))
  return { router, wrapper }
}

describe('TasksPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.setItem('ws.mock.session', 'admin')
    setLiveSourceFactory(() => new WorldLiveSource())
  })
  afterEach(() => {
    document.body.innerHTML = ''
    localStorage.clear()
  })

  it('tells non-admins the page is for administrators', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/tasks', component: TasksPage }] })
    useAuthStore().session = session(['stats.read'])
    await router.push('/tasks')
    const Shell = defineComponent({ render: () => h(QLayout, () => h(QPageContainer, () => h(TasksPage))) })
    const wrapper = mount(Shell, { attachTo: document.body, global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.text()).toContain('Tasks and workers are for administrators.')
    wrapper.unmount()
  })

  it('lists scheduled tasks with their schedule and owner', async () => {
    const { wrapper } = await setup('/tasks')
    await vi.waitFor(() => expect(wrapper.text()).toContain('Blob garbage collection'))
    expect(wrapper.text()).toContain('Every 10 seconds')
    expect(wrapper.text()).toMatch(/Policy [a-z0-9-]+/)
    wrapper.unmount()
  })

  it('opens a run with its log from the runs tab', async () => {
    const { wrapper, router } = await setup('/tasks?tab=runs')
    await vi.waitFor(() => expect(wrapper.findAll('tbody tr').length).toBeGreaterThan(1))
    await wrapper.findAll('tbody tr')[0]!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.run).toBeTruthy()
    await vi.waitFor(() => expect(document.body.querySelector('[role="log"]')).not.toBeNull())
    wrapper.unmount()
  })

  it('retries a dead letter', async () => {
    const { wrapper } = await setup('/tasks?tab=dead')
    const retryButton = () => wrapper.findAll('button').find((b) => b.text().includes('Retry') && b.attributes('disabled') === undefined && b.attributes('aria-disabled') !== 'true')
    await vi.waitFor(() => expect(retryButton()).toBeTruthy())
    const before = wrapper.findAll('tbody tr').length
    const retry = retryButton()!
    await retry.trigger('click')
    await vi.waitFor(() => expect(wrapper.findAll('tbody tr').length).toBe(before - 1))
    wrapper.unmount()
  })
})
