// SPDX-License-Identifier: GPL-3.0-or-later
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveEvent, Transfer } from '@/api/types'

import { FAILED_KEEP_MS, FINISHED_KEEP_MS, useLiveStore } from './live'
import { type LiveHandlers, type LiveSource, setLiveSourceFactory } from './liveSource'

const ts = () => new Date().toISOString()

function transfer(id: string, extra: Partial<Transfer> = {}): Transfer {
  return {
    id,
    repository: 'docker-hub',
    format: 'docker',
    direction: 'download',
    artifact: `library/nginx@sha256:${id}`,
    client: { ip: '192.0.2.10' },
    cache: 'FETCHING',
    followers: 0,
    bytes_done: 0,
    bytes_total: 1000,
    rate_bps: 0,
    started_at: ts(),
    updated_at: ts(),
    state: 'active',
    ...extra,
  }
}

class FakeSource implements LiveSource {
  static last: FakeSource | null = null
  topics: string[] = []
  handlers: LiveHandlers | null = null
  closed = 0
  open(topics: string[], _id: string | null, handlers: LiveHandlers): void {
    FakeSource.last = this
    this.topics = topics
    this.handlers = handlers
    handlers.onStatus('open')
  }
  close(): void {
    this.closed++
  }
  emit(event: LiveEvent, id: string | null = null): void {
    this.handlers?.onEvent(event, id)
  }
}

describe('live store', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setActivePinia(createPinia())
    setLiveSourceFactory(() => new FakeSource())
  })

  afterEach(() => {
    useLiveStore().reset()
    vi.useRealTimers()
  })

  it('applies started/progress/finished and publishes at most every flush', () => {
    const live = useLiveStore()
    live.apply({ type: 'transfer.started', ts: ts(), transfer: transfer('a') })
    expect(live.transfers).toHaveLength(0) // not flushed yet
    live.flush()
    expect(live.transfers).toHaveLength(1)
    expect(live.activeCount).toBe(1)

    for (let i = 1; i <= 10; i++) live.apply({ type: 'transfer.progress', ts: ts(), id: 'a', bytes_done: i * 50, rate_bps: 1000, followers: 2 })
    live.flush()
    expect(live.transfers[0]).toMatchObject({ bytes_done: 500, rate_bps: 1000, followers: 2 })

    live.apply({ type: 'transfer.finished', ts: ts(), id: 'a', bytes_done: 1000, duration_ms: 500, cache: 'FETCHING' })
    live.flush()
    expect(live.transfers[0]).toMatchObject({ state: 'finished', bytes_done: 1000, rate_bps: 2000 })
    expect(live.activeCount).toBe(0)
  })

  it('fades finished rows after 5 s and keeps failures for 30 s', () => {
    const live = useLiveStore()
    live.apply({ type: 'transfer.started', ts: ts(), transfer: transfer('ok') })
    live.apply({ type: 'transfer.started', ts: ts(), transfer: transfer('bad') })
    live.apply({ type: 'transfer.finished', ts: ts(), id: 'ok', bytes_done: 1000, duration_ms: 10 })
    live.apply({ type: 'transfer.failed', ts: ts(), id: 'bad', error: 'upstream reset' })
    const t0 = Date.now()
    live.flush(t0)
    expect(live.transfers.map((x) => x.id).sort()).toEqual(['bad', 'ok'])
    expect(live.transfers.find((x) => x.id === 'bad')).toMatchObject({ state: 'failed', cache: 'ERROR', error: 'upstream reset' })

    live.flush(t0 + FINISHED_KEEP_MS)
    expect(live.transfers.map((x) => x.id)).toEqual(['bad'])
    live.flush(t0 + FAILED_KEEP_MS)
    expect(live.transfers).toHaveLength(0)
  })

  it('replaces active state from a snapshot and ignores unknown progress', () => {
    const live = useLiveStore()
    live.apply({ type: 'transfer.started', ts: ts(), transfer: transfer('old') })
    live.apply({ type: 'transfers.snapshot', ts: ts(), transfers: [transfer('x'), transfer('y')] })
    live.apply({ type: 'transfer.progress', ts: ts(), id: 'nope', bytes_done: 5, rate_bps: 1 })
    live.flush()
    expect(live.transfers.map((x) => x.id).sort()).toEqual(['x', 'y'])
  })

  it('keeps stats ticks and de-duplicates activity', () => {
    const live = useLiveStore()
    const tick: LiveEvent = { type: 'stats.tick', ts: ts(), requests_per_s: 12, bytes_out_per_s: 1, bytes_upstream_per_s: 0, hit_ratio_1m: 0.9, active_transfers: 3 }
    live.apply(tick)
    expect(live.lastTick).toEqual(tick)
    expect(live.ticks).toHaveLength(1)
    const event = { id: '1-0', ts: ts(), kind: 'cleanup' as const, summary: 'Cleanup ran', outcome: 'success' as const }
    live.apply({ type: 'activity', ts: ts(), event }, '1-0')
    live.apply({ type: 'activity', ts: ts(), event }, '1-0')
    expect(live.activity).toHaveLength(1)
  })

  it('opens one source with the union of topics and reopens when they change', async () => {
    const live = useLiveStore()
    const offA = live.subscribe('transfers')
    const offB = live.subscribe('repo:debian')
    await vi.advanceTimersByTimeAsync(1)
    expect(FakeSource.last?.topics).toEqual(['repo:debian', 'transfers'])
    expect(live.status).toBe('open')

    offB()
    await vi.advanceTimersByTimeAsync(1)
    expect(FakeSource.last?.topics).toEqual(['transfers'])

    FakeSource.last?.emit({ type: 'transfer.started', ts: ts(), transfer: transfer('z') })
    await vi.advanceTimersByTimeAsync(300)
    expect(live.transfers.map((x) => x.id)).toEqual(['z'])

    offA()
    await vi.advanceTimersByTimeAsync(1)
    expect(live.status).toBe('idle')
  })
})
