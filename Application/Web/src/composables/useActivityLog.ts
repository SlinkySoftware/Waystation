// SPDX-License-Identifier: GPL-3.0-or-later
// The Activity page's log: history pages from `GET /activity` (cursor paging into the past) plus the live tail from
// the shared event stream, filtered the same way. Live events are buffered and applied at 4 Hz; while the user has
// paused (or scrolled away from the newest rows) they collect in `pending` until resumed.
import { computed, onScopeDispose, ref, shallowRef, toValue, watch, type MaybeRefOrGetter } from 'vue'

import { api, call } from '@/api/client'
import type { ActivityEvent } from '@/api/types'
import { useLiveTopic } from '@/composables/useLiveTopic'
import { useLiveStore } from '@/stores/live'
import { type ActivityFilters, canFollow, matchesQuery, mergeEvents, toApiQuery } from '@/utils/activity'

export const PAGE_SIZE = 100
export const MAX_ROWS = 5000
export const PENDING_MAX = 2000
export const APPLY_MS = 250

export function useActivityLog(filters: MaybeRefOrGetter<ActivityFilters>) {
  const live = useLiveStore()
  useLiveTopic(['activity'])

  const items = shallowRef<ActivityEvent[]>([])
  const pending = shallowRef<ActivityEvent[]>([])
  /** Pending events dropped beyond PENDING_MAX (they stay reachable through history paging). */
  const overflow = ref(0)
  const loading = ref(false)
  const error = ref<unknown>(null)
  const done = ref(false)
  /** Paused by the user (button). */
  const paused = ref(false)
  /** Held because the user scrolled away from the newest rows. */
  const held = ref(false)

  let cursor: string | null = null
  let generation = 0
  let buffer: ActivityEvent[] = []

  const followable = computed(() => canFollow(toValue(filters)))
  const following = computed(() => followable.value && !paused.value && !held.value)
  const liveStatus = computed(() => live.status)

  async function fetchPage(mine: number): Promise<void> {
    loading.value = true
    try {
      const query = { ...toApiQuery(toValue(filters)), limit: PAGE_SIZE, cursor: cursor ?? undefined }
      const page = await call(api.GET('/activity', { params: { query } }))
      if (mine !== generation) return
      items.value = mergeEvents(items.value, page.items)
      cursor = page.next_cursor
      done.value = page.next_cursor === null
      error.value = null
    } catch (cause) {
      if (mine === generation) error.value = cause
    } finally {
      if (mine === generation) loading.value = false
    }
  }

  /** Start over (filters changed, or Retry). */
  async function reload(): Promise<void> {
    const mine = ++generation
    items.value = []
    pending.value = []
    overflow.value = 0
    buffer = []
    cursor = null
    done.value = false
    held.value = false
    await fetchPage(mine)
  }

  /** The next page into the past; no-op while loading or at the end. */
  async function loadMore(): Promise<void> {
    if (loading.value || done.value || error.value) return
    await fetchPage(generation)
  }

  function trim(list: ActivityEvent[]): ActivityEvent[] {
    if (list.length <= MAX_ROWS) return list
    const kept = list.slice(0, MAX_ROWS)
    // Cursors are exclusive stream ids: continue history right after the last row we keep.
    cursor = kept[kept.length - 1]!.id
    done.value = false
    return kept
  }

  function apply(): void {
    if (!buffer.length) return
    const incoming = buffer
    buffer = []
    if (following.value) {
      items.value = trim(mergeEvents(items.value, incoming))
    } else {
      const merged = mergeEvents(pending.value, incoming)
      overflow.value += Math.max(0, merged.length - PENDING_MAX)
      pending.value = merged.slice(0, PENDING_MAX)
    }
  }

  /** Apply everything that arrived while paused or held, and follow again. */
  function resume(): void {
    paused.value = false
    held.value = false
    items.value = trim(mergeEvents(items.value, pending.value))
    pending.value = []
    overflow.value = 0
  }

  function pause(): void {
    paused.value = true
  }

  /** The list reports whether its newest rows are in view. */
  function setAtTop(atTop: boolean): void {
    if (atTop && held.value) {
      held.value = false
      if (!paused.value) resume()
    } else if (!atTop) {
      held.value = true
    }
  }

  const offEvents = live.onActivity((event) => {
    const f = toValue(filters)
    if (canFollow(f) && matchesQuery(event, toApiQuery(f))) buffer.push(event)
  })
  const timer = setInterval(apply, APPLY_MS)
  onScopeDispose(() => {
    offEvents()
    clearInterval(timer)
  })

  watch(() => JSON.stringify(toValue(filters)), () => void reload(), { immediate: true })

  return { items, pending, overflow, loading, error, done, paused, held, following, followable, liveStatus, reload, loadMore, pause, resume, setAtTop, apply }
}
