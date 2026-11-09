// SPDX-License-Identifier: GPL-3.0-or-later
// Live "would remove N items" totals for a draft policy: a `limit: 0` preview (totals only), debounced while the
// criteria change, ignoring stale responses. Field errors (422) are exposed so the editor can mark criteria.
import { onScopeDispose, ref, shallowRef, watch } from 'vue'

import { api, ApiError, call } from '@/api/client'
import type { CleanupPreview, CleanupPreviewRequest } from '@/api/types'
import { hasSelector } from '@/components/cleanup/cleanup'

export type PreviewScope = Omit<CleanupPreviewRequest, 'cursor' | 'limit'>

export function useCleanupPreview(request: () => PreviewScope | null, delay = 400) {
  const totals = shallowRef<CleanupPreview | null>(null)
  const loading = ref(false)
  const error = ref<unknown>(null)
  const fieldErrors = ref<Record<string, string>>({})
  /** False when the criteria select nothing (no request is made). */
  const ready = ref(false)
  let timer: ReturnType<typeof setTimeout> | null = null
  let generation = 0

  async function run(body: PreviewScope): Promise<void> {
    const mine = ++generation
    loading.value = true
    try {
      const value = await call(api.POST('/cleanup-policies/preview', { body: { ...body, limit: 0 } }))
      if (mine !== generation) return
      totals.value = value
      error.value = null
      fieldErrors.value = {}
    } catch (cause) {
      if (mine !== generation) return
      totals.value = null
      error.value = cause
      fieldErrors.value = cause instanceof ApiError ? cause.fieldErrors : {}
    } finally {
      if (mine === generation) loading.value = false
    }
  }

  watch(
    () => JSON.stringify(request()),
    () => {
      if (timer) clearTimeout(timer)
      const body = request()
      ready.value = Boolean(body && hasSelector(body.criteria))
      if (!body || !ready.value) {
        generation++
        totals.value = null
        loading.value = false
        error.value = null
        fieldErrors.value = {}
        return
      }
      loading.value = true
      timer = setTimeout(() => void run(body), delay)
    },
    { immediate: true },
  )
  onScopeDispose(() => {
    if (timer) clearTimeout(timer)
  })

  return { totals, loading, error, fieldErrors, ready }
}
