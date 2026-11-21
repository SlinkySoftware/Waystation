// SPDX-License-Identifier: GPL-3.0-or-later
// Lazy folder tree over `GET /repositories/{name}/browse` (T-U05): folders load on expand, page by page (500 per
// request); the visible rows are a flat list (one per expanded node) for the virtualised tree. Entries are kept in
// plain, non-reactive maps (a folder can hold 100,000 files) and a version counter tells Vue when to re-flatten.
import { computed, markRaw, ref, type Ref } from 'vue'

import type { FolderEntry } from '@/api/types'

export const PAGE = 500

/** Reads one page of a folder; the composable never calls the API itself (tests pass a fake). */
export type FetchPage = (path: string, cursor: string | null) => Promise<{ entries: FolderEntry[]; next_cursor: string | null }>

export interface FolderState {
  entries: FolderEntry[]
  cursor: string | null
  loaded: boolean
  loading: boolean
  error: unknown
}

export type TreeRow =
  | { type: 'node'; key: string; path: string; name: string; kind: 'folder' | 'file'; level: number; setsize: number; posinset: number; entry: FolderEntry | null }
  | { type: 'more'; key: string; path: string; level: number; loading: boolean; failed: boolean }

/** The folder that holds `path` ('' for top-level entries). */
export function parentOf(path: string): string {
  const slash = path.lastIndexOf('/')
  return slash < 0 ? '' : path.slice(0, slash)
}

/** `a/b/c` → ['', 'a', 'a/b']: the folders to open so that `path` shows. */
export function ancestorsOf(path: string): string[] {
  if (!path) return []
  const parts = path.split('/')
  return ['', ...parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join('/'))]
}

export function useFolderTree(fetchPage: FetchPage, rootName: Ref<string>) {
  const folders = new Map<string, FolderState>()
  const expanded = new Set<string>([''])
  const version = ref(0)
  const bump = () => (version.value += 1)

  function state(path: string): FolderState {
    let s = folders.get(path)
    if (!s) {
      s = markRaw({ entries: [], cursor: null, loaded: false, loading: false, error: null })
      folders.set(path, s)
    }
    return s
  }

  /** Load the first page (or, with `more`, the next page) of `path`. Concurrent calls share one request. */
  const inflight = new Map<string, Promise<void>>()
  function load(path: string, more = false): Promise<void> {
    const s = state(path)
    if (s.loaded && (!more || s.cursor === null)) return Promise.resolve()
    const running = inflight.get(path)
    if (running) return running
    s.loading = true
    s.error = null
    bump()
    const job = fetchPage(path, more ? s.cursor : null)
      .then((page) => {
        s.entries = more ? s.entries.concat(page.entries) : page.entries
        s.cursor = page.next_cursor
        s.loaded = true
      })
      .catch((error: unknown) => {
        s.error = error
      })
      .finally(() => {
        s.loading = false
        inflight.delete(path)
        bump()
      })
    inflight.set(path, job)
    return job
  }

  async function expand(path: string): Promise<void> {
    if (!expanded.has(path)) {
      expanded.add(path)
      bump()
    }
    await load(path)
  }

  function collapse(path: string): void {
    if (path !== '' && expanded.delete(path)) bump()
  }

  function isExpanded(path: string): boolean {
    return expanded.has(path)
  }

  /** The entry at `path`, loading pages of its parent until it shows (bounded by the folder's size). */
  async function find(path: string): Promise<FolderEntry | null> {
    const parent = parentOf(path)
    await load(parent)
    for (;;) {
      const s = state(parent)
      const hit = s.entries.find((e) => e.path === path)
      if (hit || s.cursor === null || s.error) return hit ?? null
      await load(parent, true)
    }
  }

  /** Open every folder above `path` so that it is visible. */
  async function reveal(path: string): Promise<FolderEntry | null> {
    for (const folder of ancestorsOf(path)) await expand(folder)
    return path ? find(path) : null
  }

  function reset(): void {
    folders.clear()
    expanded.clear()
    expanded.add('')
    bump()
  }

  const rows = computed<TreeRow[]>(() => {
    void version.value
    const out: TreeRow[] = []
    out.push({ type: 'node', key: '/', path: '', name: rootName.value, kind: 'folder', level: 1, setsize: 1, posinset: 1, entry: null })
    const walk = (path: string, level: number): void => {
      if (!expanded.has(path)) return
      const s = folders.get(path)
      if (!s) return
      const total = s.entries.length + (s.cursor !== null ? 1 : 0)
      for (let i = 0; i < s.entries.length; i++) {
        const e = s.entries[i]!
        out.push({ type: 'node', key: e.path, path: e.path, name: e.name, kind: e.kind, level, setsize: total, posinset: i + 1, entry: e })
        if (e.kind === 'folder') walk(e.path, level + 1)
      }
      if (s.cursor !== null || s.loading || s.error) {
        out.push({ type: 'more', key: `${path}/…more`, path, level, loading: s.loading, failed: Boolean(s.error) })
      }
    }
    walk('', 2)
    return out
  })

  return { rows, state, load, expand, collapse, isExpanded, reveal, find, reset, version }
}

export type FolderTree = ReturnType<typeof useFolderTree>
