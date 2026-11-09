// SPDX-License-Identifier: GPL-3.0-or-later
// Recently opened repositories, search results and queries (command palette "Recent"), kept in localStorage.
import { acceptHMRUpdate, defineStore } from 'pinia'
import { ref } from 'vue'

import type { Format } from '@/api/types'
import { readString, writeString } from '@/utils/storage'

export type RecentKind = 'repository' | 'hit' | 'query'

/** A route the palette can open again (plain JSON: stored as is). */
export interface RecentRoute {
  name: string
  params?: Record<string, string>
  query?: Record<string, string | string[]>
}

export interface RecentItem {
  /** Identity: re-opening the same thing moves it to the top instead of adding a duplicate. */
  id: string
  kind: RecentKind
  label: string
  detail?: string
  format?: Format
  repository?: string
  to: RecentRoute
  at: number
}

const KEY = 'ws.recent'
export const RECENT_MAX = 8

function load(): RecentItem[] {
  try {
    const parsed: unknown = JSON.parse(readString(KEY) ?? '[]')
    if (!Array.isArray(parsed)) return []
    return parsed.filter((i): i is RecentItem => !!i && typeof i === 'object' && typeof (i as RecentItem).id === 'string' && typeof (i as RecentItem).label === 'string' && !!(i as RecentItem).to).slice(0, RECENT_MAX)
  } catch {
    return []
  }
}

export const useRecentStore = defineStore('recent', () => {
  const items = ref<RecentItem[]>(load())

  function add(item: Omit<RecentItem, 'at'>): void {
    items.value = [{ ...item, at: Date.now() }, ...items.value.filter((i) => i.id !== item.id)].slice(0, RECENT_MAX)
    writeString(KEY, JSON.stringify(items.value))
  }

  function clear(): void {
    items.value = []
    writeString(KEY, null)
  }

  return { items, add, clear }
})

if (import.meta.hot) import.meta.hot.accept(acceptHMRUpdate(useRecentStore, import.meta.hot))
