// SPDX-License-Identifier: GPL-3.0-or-later
// Load data for a view: re-runs when its reactive inputs change, ignores stale responses, keeps the previous data
// while reloading (no flashing), exposes loading/error for skeletons and error states.
import { type Ref, ref, shallowRef, watch, type WatchSource } from 'vue'

export interface AsyncState<T> {
  data: Ref<T | null>
  error: Ref<unknown>
  loading: Ref<boolean>
  reload: () => Promise<void>
}

export function useAsync<T>(load: () => Promise<T>, sources: WatchSource[] = []): AsyncState<T> {
  const data = shallowRef<T | null>(null) as Ref<T | null>
  const error = ref<unknown>(null)
  const loading = ref(false)
  let generation = 0

  async function reload(): Promise<void> {
    const mine = ++generation
    loading.value = true
    try {
      const value = await load()
      if (mine !== generation) return
      data.value = value
      error.value = null
    } catch (cause) {
      if (mine !== generation) return
      error.value = cause
    } finally {
      if (mine === generation) loading.value = false
    }
  }

  watch(sources, () => void reload(), { immediate: true })
  return { data, error, loading, reload }
}
