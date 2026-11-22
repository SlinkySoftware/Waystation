// SPDX-License-Identifier: GPL-3.0-or-later
// Prefetch (T-O12): the overview card (states, "Warm now", the run link) and the form fields (count, errors).
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { PrefetchStatus, Repository } from '@/api/types'
import { seedRepositories } from '@/mocks/fixtures/repositories'
import { CAPS, signInAs } from '@/test/session'

import { emptyModel, type RepoFormModel } from './form'
import PrefetchCard from './PrefetchCard.vue'
import PrefetchFields from './PrefetchFields.vue'

const get = vi.hoisted(() => vi.fn())
const post = vi.hoisted(() => vi.fn())
const notify = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))

vi.mock('@/composables/useNotify', () => ({ useNotify: () => notify }))

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { GET: get, POST: post }, call: async <T,>(promise: Promise<T>) => promise }
})

const hub = seedRepositories().find((r) => r.name === 'dockerhub-proxy')!

function status(partial: Partial<PrefetchStatus> = {}): PrefetchStatus {
  return {
    warm_supported: true,
    refresh_supported: true,
    warm_items: 4,
    refresh_popular: true,
    schedule: '0 3 * * *',
    next_run_at: new Date(Date.now() + 5 * 3_600_000).toISOString(),
    last_run: { id: 'pw_1', status: 'succeeded', trigger: 'schedule', started_at: new Date(Date.now() - 3_600_000).toISOString(), finished_at: new Date(Date.now() - 3_590_000).toISOString(), items: 4, failed: 1, files: 38, fetched: 7, error: null },
    ...partial,
  }
}

function router() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/repositories/:name/:tab?', name: 'repository', component: { template: '<div />' } },
      { path: '/tasks', name: 'tasks', component: { template: '<div />' } },
    ],
  })
}

async function mountCard(repo: Repository = hub) {
  const w = mount(PrefetchCard, { props: { repo }, global: { plugins: [router()] } })
  await flushPromises()
  return w
}

describe('PrefetchCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    get.mockReset()
    post.mockReset()
    notify.success.mockReset()
    notify.error.mockReset()
    signInAs(CAPS.admin, 'admin')
  })

  it('shows refresh, the warm list, the schedule and the last run with a link to it on the Tasks page', async () => {
    get.mockResolvedValue(status())
    const w = await mountCard()
    expect(get).toHaveBeenCalledWith('/repositories/{name}/prefetch', { params: { path: { name: 'dockerhub-proxy' } } })
    expect(w.find('[data-test="prefetch-refresh-state"]').text()).toContain('Kept fresh')
    expect(w.find('[data-test="prefetch-items"]').text()).toContain('4 items')
    expect(w.find('[data-test="prefetch-schedule-state"]').text()).toContain('Daily at 03:00 UTC')
    expect(w.find('[data-test="prefetch-schedule-state"]').text()).toContain('Next run')
    const last = w.find('[data-test="prefetch-last-run"]')
    expect(last.text()).toContain('Succeeded')
    expect(last.text()).toContain('4 items · 1 failed · 7 of 38 files fetched upstream')
    expect(w.find('[data-test="prefetch-run-link"]').attributes('href')).toBe('/tasks?run=pw_1')
  })

  it('reads "off", no items, manual only and never ran; Warm now waits for a warm list', async () => {
    get.mockResolvedValue(status({ refresh_popular: false, warm_items: 0, schedule: null, next_run_at: null, last_run: null }))
    const w = await mountCard()
    expect(w.find('[data-test="prefetch-refresh-state"]').text()).toContain('Off')
    expect(w.find('[data-test="prefetch-items"]').text()).toContain('No items')
    expect(w.find('[data-test="prefetch-schedule-state"]').text()).toContain('Only when run by hand')
    expect(w.find('[data-test="prefetch-last-run"]').text()).toContain('Never ran')
    expect(w.find('[data-test="prefetch-warm-now"]').attributes('disabled')).toBeDefined()
  })

  it('hides the run link from managers who cannot open the Tasks page', async () => {
    signInAs(['repositories.browse', 'repositories.manage'], 'mia')
    get.mockResolvedValue(status())
    const w = await mountCard()
    expect(w.find('[data-test="prefetch-last-run"]').text()).toContain('Succeeded')
    expect(w.find('[data-test="prefetch-run-link"]').exists()).toBe(false)
  })

  it('is hidden when the format supports neither, or the server says it is not a proxy (409)', async () => {
    get.mockResolvedValue(status({ warm_supported: false, refresh_supported: false, warm_items: 0, last_run: null }))
    expect((await mountCard()).find('[data-test="prefetch-card"]').exists()).toBe(false)
    const { ApiError } = await import('@/api/client')
    get.mockRejectedValue(new ApiError({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'not a proxy' }))
    expect((await mountCard()).find('[data-test="prefetch-card"]').exists()).toBe(false)
  })

  it('shows a load failure with a retry', async () => {
    get.mockRejectedValue(new Error('network down'))
    const w = await mountCard()
    expect(w.text()).toContain('Could not load the prefetch status')
    get.mockResolvedValue(status())
    await w.findAll('button').find((b) => b.text() === 'Retry')!.trigger('click')
    await flushPromises()
    expect(w.find('[data-test="prefetch-items"]').text()).toContain('4 items')
  })

  it('warms now, reports queued or joined, and shows the server refusal', async () => {
    get.mockResolvedValue(status())
    post.mockResolvedValue({ job_id: 'job-1', status: 'enqueued' })
    const w = await mountCard()
    await w.find('[data-test="prefetch-warm-now"]').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/repositories/{name}/prefetch/run', { params: { path: { name: 'dockerhub-proxy' } } })
    expect(get).toHaveBeenCalledTimes(2) // reloaded after queueing
    expect(notify.success).toHaveBeenLastCalledWith('Warm run of dockerhub-proxy queued')

    post.mockResolvedValue({ job_id: 'job-1', status: 'merged' })
    await w.find('[data-test="prefetch-warm-now"]').trigger('click')
    await flushPromises()
    expect(notify.success).toHaveBeenLastCalledWith('A warm run of dockerhub-proxy is already queued; this request joined it')

    const { ApiError } = await import('@/api/client')
    const refusal = new ApiError({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'dockerhub-proxy has no warm list' })
    post.mockRejectedValue(refusal)
    await w.find('[data-test="prefetch-warm-now"]').trigger('click')
    await flushPromises()
    expect(notify.error).toHaveBeenLastCalledWith(refusal)
  })
})

function mountFields(model: RepoFormModel, errors: Record<string, string> = {}) {
  const state = ref(model)
  const Host = defineComponent({ setup: () => () => h(PrefetchFields, { modelValue: state.value, 'onUpdate:modelValue': (v: RepoFormModel) => (state.value = v), errors }) })
  return { w: mount(Host), state }
}

describe('PrefetchFields', () => {
  it('uses the format syntax as placeholder and help, and counts the items', async () => {
    const { w } = mountFields({ ...emptyModel('npm'), warmText: 'lodash@4.17.21\n\nreact@18.3.1\nlodash@4.17.21' })
    expect(w.find('textarea').attributes('placeholder')).toContain('lodash@4.17.21')
    expect(w.text()).toContain('name, name@version or name@dist-tag (no ranges)')
    expect(w.find('[data-test="prefetch-count"]').text()).toBe('2 of 200 items')
    expect(mountFields(emptyModel('maven')).w.text()).toContain('groupId:artifactId:version')
  })

  it('names the item a server field error is about, and shows schedule and refresh errors', () => {
    const model = { ...emptyModel('npm'), warmText: 'lodash@4.17.21\nreact@^18', prefetchSchedule: '@every 5m' }
    const { w } = mountFields(model, {
      'proxy.prefetch.warm.1': 'name, name@version or name@dist-tag (no ranges)',
      'proxy.prefetch.schedule': 'not a valid schedule: a warm list runs at most every 15 minutes',
      'proxy.prefetch.refresh_popular': 'not supported',
    })
    expect(w.text()).toContain('"react@^18": name, name@version or name@dist-tag (no ranges)')
    expect(w.text()).toContain('a warm list runs at most every 15 minutes')
    expect(w.text()).toContain('not supported')
  })

  it('reads a valid schedule in words and fills an example', async () => {
    const { w, state } = mountFields({ ...emptyModel('docker'), prefetchSchedule: '0 3 * * *' })
    expect(w.text()).toContain('Runs daily at 03:00 UTC')
    const chip = w.findAll('.q-chip').find((c) => c.text() === '@every 6h')!
    await chip.trigger('click')
    expect(state.value.prefetchSchedule).toBe('@every 6h')
  })
})
