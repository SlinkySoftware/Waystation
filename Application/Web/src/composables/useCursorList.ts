// SPDX-License-Identifier: GPL-3.0-or-later
// A keyset-paged list ("Load more"): reloads from the first page when its inputs change, appends further pages,
// ignores stale responses, keeps rows while reloading (no flashing).
import { type Ref, ref, shallowRef, watch, type WatchSource } from 'vue'

export interface CursorPage<T> {
  items: T[]
  next_cursor: string | null
}

export interface CursorList<T> {
  items: Ref<T[]>
  nextCursor: Ref<string | null>
  loading: Ref<boolean>
  error: Ref<unknown>
  reload: () => Promise<void>
  loadMore: () => Promise<void>
}

export function useCursorList<T>(load: (cursor: string | null) => Promise<CursorPage<T>>, sources: WatchSource[] = []): CursorList<T> {
  const items = shallowRef<T[]>([]) as Ref<T[]>
  const nextCursor = ref<string | null>(null)
  const loading = ref(false)
  const error = ref<unknown>(null)
  let generation = 0

  async function fetchPage(cursor: string | null): Promise<void> {
    const mine = ++generation
    loading.value = true
    try {
      const page = await load(cursor)
      if (mine !== generation) return
      items.value = cursor ? [...items.value, ...page.items] : page.items
      nextCursor.value = page.next_cursor
      error.value = null
    } catch (cause) {
      if (mine !== generation) return
      error.value = cause
      if (!cursor) items.value = []
    } finally {
      if (mine === generation) loading.value = false
    }
  }

  watch(sources, () => void fetchPage(null), { immediate: true })
  return {
    items,
    nextCursor,
    loading,
    error,
    reload: () => fetchPage(null),
    loadMore: () => (nextCursor.value ? fetchPage(nextCursor.value) : Promise.resolve()),
  }
}
