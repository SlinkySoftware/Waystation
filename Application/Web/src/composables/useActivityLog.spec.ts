// SPDX-License-Identifier: GPL-3.0-or-later
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'

import type { ActivityEvent } from '@/api/types'
import { useLiveStore } from '@/stores/live'
import { type LiveSource, setLiveSourceFactory } from '@/stores/liveSource'
import { type ActivityFilters, NO_FILTERS } from '@/utils/activity'

import { APPLY_MS, MAX_ROWS, useActivityLog } from './useActivityLog'

interface Query {
  cursor?: string
  limit?: number
  repo?: string
}

const calls: Query[] = []
let pages: ((q: Query) => { items: ActivityEvent[]; next_cursor: string | null }) | null = null

vi.mock('@/api/client', () => ({
  api: {
    GET: (_path: string, init: { params: { query: Query } }) => {
      calls.push(init.params.query)
      return Promise.resolve({ data: pages!(init.params.query), response: new Response(null, { status: 200 }) })
    },
  },
  call: async <T>(p: Promise<{ data?: T }>) => (await p).data as T,
}))

class QuietSource implements LiveSource {
  open(): void {}
  close(): void {}
}

function ev(ms: number, extra: Partial<ActivityEvent> = {}): ActivityEvent {
  return { id: `${ms}-0`, ts: new Date(ms).toISOString(), kind: 'download', summary: `e${ms}`, outcome: 'success', repository: 'npm', format: 'npm', principal: 'alice', cache: 'HIT', ...extra }
}

const NOW = Date.parse('2026-11-09T12:00:00Z')
const history = Array.from({ length: 250 }, (_, i) => ev(NOW - 1000 - i * 1000))

function serveHistory(): void {
  pages = (q) => {
    const from = q.cursor ? history.findIndex((e) => e.id === q.cursor) + 1 : 0
    const matching = history.slice(from).filter((e) => !q.repo || e.repository === q.repo)
    const items = matching.slice(0, q.limit ?? 50)
    return { items, next_cursor: matching.length > items.length ? items[items.length - 1]!.id : null }
  }
}

async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) {
    await Promise.resolve()
    await nextTick()
  }
}

function setup(filters = ref<ActivityFilters>({ ...NO_FILTERS })) {
  const scope = effectScope()
  const log = scope.run(() => useActivityLog(filters))!
  return { log, scope, filters, live: useLiveStore() }
}

function emit(live: ReturnType<typeof useLiveStore>, e: ActivityEvent): void {
  live.apply({ type: 'activity', ts: e.ts, event: e }, e.id)
}

describe('useActivityLog', () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: NOW })
    setActivePinia(createPinia())
    setLiveSourceFactory(() => new QuietSource())
    calls.length = 0
    serveHistory()
  })

  afterEach(() => {
    useLiveStore().reset()
    vi.useRealTimers()
  })

  it('loads the newest page, then pages back with the cursor until the end', async () => {
    const { log, scope } = setup()
    await settle()
    expect(log.items.value).toHaveLength(100)
    expect(log.items.value[0]!.id).toBe(history[0]!.id)
    await log.loadMore()
    await log.loadMore()
    expect(calls.map((c) => c.cursor)).toEqual([undefined, history[99]!.id, history[199]!.id])
    expect(log.items.value).toHaveLength(250)
    expect(log.done.value).toBe(true)
    await log.loadMore()
    expect(calls).toHaveLength(3)
    scope.stop()
  })

  it('follows the tail: matching live events appear at the top, batched', async () => {
    const { log, scope, live } = setup()
    await settle()
    emit(live, ev(NOW + 1))
    emit(live, ev(NOW + 2, { repository: 'debian' }))
    expect(log.items.value[0]!.id).toBe(history[0]!.id) // not applied before the next batch
    vi.advanceTimersByTime(APPLY_MS)
    expect(log.items.value.slice(0, 2).map((e) => e.id)).toEqual([`${NOW + 2}-0`, `${NOW + 1}-0`])
    expect(log.following.value).toBe(true)
    scope.stop()
  })

  it('holds new events while paused and applies them on resume', async () => {
    const { log, scope, live } = setup()
    await settle()
    log.pause()
    expect(log.following.value).toBe(false)
    emit(live, ev(NOW + 1))
    emit(live, ev(NOW + 2))
    vi.advanceTimersByTime(APPLY_MS)
    expect(log.items.value[0]!.id).toBe(history[0]!.id)
    expect(log.pending.value.map((e) => e.id)).toEqual([`${NOW + 2}-0`, `${NOW + 1}-0`])
    log.resume()
    expect(log.pending.value).toHaveLength(0)
    expect(log.items.value[0]!.id).toBe(`${NOW + 2}-0`)
    expect(log.following.value).toBe(true)
    scope.stop()
  })

  it('holds while scrolled away from the top and catches up when back', async () => {
    const { log, scope, live } = setup()
    await settle()
    log.setAtTop(false)
    emit(live, ev(NOW + 5))
    vi.advanceTimersByTime(APPLY_MS)
    expect(log.held.value).toBe(true)
    expect(log.pending.value).toHaveLength(1)
    expect(log.items.value[0]!.id).toBe(history[0]!.id)
    log.setAtTop(true)
    expect(log.held.value).toBe(false)
    expect(log.items.value[0]!.id).toBe(`${NOW + 5}-0`)
    // A user pause survives scrolling back to the top.
    log.pause()
    log.setAtTop(false)
    log.setAtTop(true)
    expect(log.paused.value).toBe(true)
    scope.stop()
  })

  it('filters live events like the server and does not follow a window that ended', async () => {
    const filters = ref<ActivityFilters>({ ...NO_FILTERS, repo: 'npm' })
    const { log, scope, live } = setup(filters)
    await settle()
    emit(live, ev(NOW + 1, { repository: 'debian' }))
    emit(live, ev(NOW + 2))
    vi.advanceTimersByTime(APPLY_MS)
    expect(log.items.value[0]!.id).toBe(`${NOW + 2}-0`)
    expect(log.items.value.some((e) => e.repository === 'debian')).toBe(false)

    filters.value = { ...NO_FILTERS, to: new Date(NOW - 60_000).toISOString() }
    await settle()
    expect(log.followable.value).toBe(false)
    emit(live, ev(NOW + 3))
    vi.advanceTimersByTime(APPLY_MS)
    expect(log.items.value.some((e) => e.id === `${NOW + 3}-0`)).toBe(false)
    expect(log.pending.value).toHaveLength(0)
    scope.stop()
  })

  it('reloads from the top when the filters change', async () => {
    const filters = ref<ActivityFilters>({ ...NO_FILTERS })
    const { log, scope } = setup(filters)
    await settle()
    log.pause()
    filters.value = { ...NO_FILTERS, repo: 'npm' }
    await settle()
    expect(calls.at(-1)).toMatchObject({ repo: 'npm', cursor: undefined })
    expect(log.items.value).toHaveLength(100)
    scope.stop()
  })

  it('caps the rows it keeps and continues history after the last kept row', async () => {
    pages = () => ({ items: [], next_cursor: null })
    const { log, scope, live } = setup()
    await settle()
    for (let i = 1; i <= MAX_ROWS + 10; i++) emit(live, ev(NOW + i))
    vi.advanceTimersByTime(APPLY_MS)
    expect(log.items.value).toHaveLength(MAX_ROWS)
    expect(log.done.value).toBe(false)
    pages = (q) => ({ items: [ev(Number(q.cursor!.split('-')[0]) - 1)], next_cursor: null })
    await log.loadMore()
    expect(calls.at(-1)!.cursor).toBe(log.items.value[MAX_ROWS - 1]!.id)
    scope.stop()
  })
})
