// SPDX-License-Identifier: GPL-3.0-or-later
// Cached repository index for pickers, filters and format marks (the Repositories page loads its own pages).
import { acceptHMRUpdate, defineStore } from 'pinia'
import { computed, shallowRef } from 'vue'

import { api, call } from '@/api/client'
import type { Format, Repository } from '@/api/types'
import type { BrandMark } from '@/components/brandMarks'
import { formatMark, repoMark } from '@/utils/domain'

export const useRepositoriesStore = defineStore('repositories', () => {
  const items = shallowRef<Repository[]>([])
  /** True once a load completed: an empty list then means "none visible", not "still loading". */
  const loaded = shallowRef(false)
  let loading: Promise<void> | null = null

  const byName = computed(() => new Map(items.value.map((r) => [r.name, r])))

  /** Load (or refresh with `force`) the full list; repos are few enough to fetch in pages of 500. */
  function load(force = false): Promise<void> {
    if (loading && !force) return loading
    loading = (async () => {
      const out: Repository[] = []
      let cursor: string | undefined
      do {
        const page = await call(api.GET('/repositories', { params: { query: { limit: 500, cursor } } }))
        out.push(...page.items)
        cursor = page.next_cursor ?? undefined
      } while (cursor)
      items.value = out
      loaded.value = true
    })().catch((error) => {
      loading = null
      throw error
    })
    return loading
  }

  function markFor(name: string, format: Format): BrandMark {
    const repo = byName.value.get(name)
    return repo ? repoMark(repo) : formatMark(format)
  }

  function upsert(repo: Repository): void {
    const rest = items.value.filter((r) => r.name !== repo.name)
    items.value = [...rest, repo].sort((a, b) => a.name.localeCompare(b.name))
  }

  function remove(name: string): void {
    items.value = items.value.filter((r) => r.name !== name)
  }

  return { items, loaded, byName, load, markFor, upsert, remove }
})

if (import.meta.hot) import.meta.hot.accept(acceptHMRUpdate(useRepositoriesStore, import.meta.hot))
