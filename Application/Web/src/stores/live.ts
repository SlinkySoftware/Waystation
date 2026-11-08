// SPDX-License-Identifier: GPL-3.0-or-later
// Shared live state (UI_DESIGN §7): one event stream per tab multiplexing topics, reconnect + Last-Event-ID.
// Events land in plain (non-reactive) maps and are flushed to reactive state at 4 Hz, so a burst of progress
// events never causes a re-render storm.
import { acceptHMRUpdate, defineStore } from 'pinia'
import { computed, ref, shallowRef } from 'vue'

import type { ActivityEvent, LiveEvent, StatsTickEvent, Transfer, UpstreamHealth } from '@/api/types'

import { type ConnectionStatus, createLiveSource, type LiveSource } from './liveSource'

export interface LiveTransfer extends Transfer {
  /** Client clock (ms) when the transfer finished or failed; drives the fade-out. */
  endedAt: number | null
}

export const FLUSH_MS = 250
export const FINISHED_KEEP_MS = 5000
export const FAILED_KEEP_MS = 30000
export const TICK_HISTORY = 180
export const ACTIVITY_HISTORY = 50

export const useLiveStore = defineStore('live', () => {
  const status = ref<ConnectionStatus>('idle')
  const transfers = shallowRef<LiveTransfer[]>([])
  const lastTick = shallowRef<StatsTickEvent | null>(null)
  const ticks = shallowRef<StatsTickEvent[]>([])
  const activity = shallowRef<ActivityEvent[]>([])
  const upstreams = shallowRef<Record<string, UpstreamHealth>>({})

  const activeCount = computed(() => transfers.value.filter((t) => t.state === 'active').length)

  // Non-reactive working state.
  const map = new Map<string, LiveTransfer>()
  const topicRefs = new Map<string, number>()
  const activityListeners = new Set<(event: ActivityEvent) => void>()
  let dirty = false
  let lastEventId: string | null = null
  let source: LiveSource | null = null
  let flushTimer: ReturnType<typeof setInterval> | null = null
  let reopenTimer: ReturnType<typeof setTimeout> | null = null

  function apply(event: LiveEvent, id: string | null = null): void {
    const now = Date.now()
    if (id) lastEventId = id
    switch (event.type) {
      case 'transfers.snapshot':
        for (const [key, t] of map) if (t.state === 'active') map.delete(key)
        for (const t of event.transfers) map.set(t.id, { ...t, endedAt: t.state === 'active' ? null : now })
        dirty = true
        break
      case 'transfer.started':
        map.set(event.transfer.id, { ...event.transfer, endedAt: null })
        dirty = true
        break
      case 'transfer.progress': {
        const t = map.get(event.id)
        if (!t) break
        t.bytes_done = event.bytes_done
        t.rate_bps = event.rate_bps
        if (event.bytes_total !== undefined) t.bytes_total = event.bytes_total
        if (event.cache) t.cache = event.cache
        if (event.followers !== undefined) t.followers = event.followers
        if (event.upstream_latency_ms !== undefined) t.upstream_latency_ms = event.upstream_latency_ms
        t.updated_at = event.ts
        dirty = true
        break
      }
      case 'transfer.finished': {
        const t = map.get(event.id)
        if (!t) break
        t.state = 'finished'
        t.bytes_done = event.bytes_done
        t.bytes_total ??= event.bytes_done
        t.rate_bps = event.duration_ms > 0 ? (event.bytes_done * 1000) / event.duration_ms : t.rate_bps
        if (event.cache) t.cache = event.cache
        t.endedAt = now
        dirty = true
        break
      }
      case 'transfer.failed': {
        const t = map.get(event.id)
        if (!t) break
        t.state = 'failed'
        t.cache = 'ERROR'
        t.error = event.error
        if (event.bytes_done !== undefined) t.bytes_done = event.bytes_done
        t.endedAt = now
        dirty = true
        break
      }
      case 'stats.tick':
        lastTick.value = event
        ticks.value = [...ticks.value.slice(-(TICK_HISTORY - 1)), event]
        break
      case 'upstream.state':
        upstreams.value = { ...upstreams.value, [event.health.repository]: event.health }
        break
      case 'activity':
        for (const listener of activityListeners) listener(event.event)
        if (activity.value.some((a) => a.id === event.event.id)) break
        activity.value = [event.event, ...activity.value].slice(0, ACTIVITY_HISTORY)
        break
    }
  }

  /** Drop rows whose fade time has passed and publish the current state. */
  function flush(now = Date.now()): void {
    for (const [key, t] of map) {
      if (t.endedAt === null) continue
      const keep = t.state === 'failed' ? FAILED_KEEP_MS : FINISHED_KEEP_MS
      if (now - t.endedAt >= keep) {
        map.delete(key)
        dirty = true
      }
    }
    if (!dirty) return
    dirty = false
    // New objects so row components see changed props.
    transfers.value = [...map.values()].map((t) => ({ ...t })).sort((a, b) => (a.started_at < b.started_at ? 1 : -1))
  }

  function topics(): string[] {
    return [...topicRefs.keys()].sort()
  }

  function reopen(): void {
    if (reopenTimer) clearTimeout(reopenTimer)
    reopenTimer = setTimeout(() => {
      reopenTimer = null
      source?.close()
      const list = topics()
      if (!list.length) {
        stop()
        return
      }
      source ??= createLiveSource()
      source.open(list, lastEventId, {
        onEvent: (event, id) => apply(event, id),
        onStatus: (next) => {
          status.value = next
        },
      })
      flushTimer ??= setInterval(() => flush(), FLUSH_MS)
    }, 0)
  }

  /** Subscribe to a topic (`transfers`, `stats`, `activity`, `repo:<name>`); returns the unsubscribe function. */
  function subscribe(topic: string): () => void {
    const count = topicRefs.get(topic) ?? 0
    topicRefs.set(topic, count + 1)
    if (count === 0) reopen()
    let done = false
    return () => {
      if (done) return
      done = true
      const left = (topicRefs.get(topic) ?? 1) - 1
      if (left > 0) topicRefs.set(topic, left)
      else {
        topicRefs.delete(topic)
        reopen()
      }
    }
  }

  /** Every activity event as it arrives (the Activity page keeps its own, longer, filtered log). */
  function onActivity(listener: (event: ActivityEvent) => void): () => void {
    activityListeners.add(listener)
    return () => activityListeners.delete(listener)
  }

  function stop(): void {
    source?.close()
    source = null
    if (flushTimer) clearInterval(flushTimer)
    flushTimer = null
    status.value = 'idle'
  }

  /** Sign-out: forget everything. */
  function reset(): void {
    topicRefs.clear()
    stop()
    map.clear()
    transfers.value = []
    ticks.value = []
    lastTick.value = null
    activity.value = []
    upstreams.value = {}
    lastEventId = null
  }

  return { status, transfers, activeCount, lastTick, ticks, activity, upstreams, subscribe, onActivity, apply, flush, reset }
})

if (import.meta.hot) import.meta.hot.accept(acceptHMRUpdate(useLiveStore, import.meta.hot))
