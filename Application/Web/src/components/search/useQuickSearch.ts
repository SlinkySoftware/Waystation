// SPDX-License-Identifier: GPL-3.0-or-later
// The palette's live results: a debounced /search for the typed query (top hits, no facets), latest answer wins.
import { onScopeDispose, reactive, type Ref, watch } from 'vue'

import { api, call } from '@/api/client'
import type { Format } from '@/api/types'
import type { BrandMark } from '@/components/brandMarks'

import { type PaletteItem, resultItem } from './palette'

export const QUICK_LIMIT = 6
export const QUICK_DEBOUNCE_MS = 150

export function useQuickSearch(query: Ref<string>, markFor: (repository: string, format: Format) => BrandMark) {
  const state = reactive({ items: [] as PaletteItem[], loading: false })
  let timer: ReturnType<typeof setTimeout> | undefined
  let generation = 0

  async function run(q: string, mine: number): Promise<void> {
    try {
      const page = await call(api.GET('/search', { params: { query: { q, limit: QUICK_LIMIT, facets: false } } }))
      if (mine !== generation) return
      state.items = page.items.map((hit) => resultItem(hit, markFor(hit.repository, hit.format)))
    } catch {
      if (mine === generation) state.items = []
    } finally {
      if (mine === generation) state.loading = false
    }
  }

  watch(query, (value) => {
    clearTimeout(timer)
    const mine = ++generation
    const q = value.trim()
    if (q.length < 2) {
      state.items = []
      state.loading = false
      return
    }
    state.loading = true
    timer = setTimeout(() => void run(q, mine), QUICK_DEBOUNCE_MS)
  })
  onScopeDispose(() => clearTimeout(timer))
  return state
}
