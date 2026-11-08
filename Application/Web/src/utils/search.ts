// SPDX-License-Identifier: GPL-3.0-or-later
// Search page state <-> URL, API query, Browse deep links and match highlighting (SPEC §4.6, UI_DESIGN §4.5).
import type { LocationQuery, LocationQueryRaw, RouteLocationRaw } from 'vue-router'

import type { Format, RepositoryType, SearchHit, SearchKind } from '@/api/types'

export const SEARCH_KINDS: readonly SearchKind[] = ['package', 'image', 'file']
export const DIGEST_RE = /^(?:sha256:[0-9a-f]{8,64}|[0-9a-f]{64})$/i

export interface SearchState {
  q: string
  format: Format[]
  repository: string[]
  kind: SearchKind[]
  type: RepositoryType[]
  cached: boolean
}

export const FACETS = ['format', 'repository', 'kind', 'type'] as const
export type FacetName = (typeof FACETS)[number]

function list(value: LocationQuery[string] | undefined): string[] {
  const values = Array.isArray(value) ? value : value == null ? [] : [value]
  return [...new Set(values.filter((v): v is string => typeof v === 'string' && v !== ''))]
}

/** Search state from the route query (`?q=&format=npm&format=apt&cached=1`). */
export function stateFromQuery(query: LocationQuery): SearchState {
  const q = query.q
  return {
    q: typeof q === 'string' ? q : '',
    format: list(query.format) as Format[],
    repository: list(query.repository),
    kind: list(query.kind).filter((k): k is SearchKind => (SEARCH_KINDS as readonly string[]).includes(k)),
    type: list(query.type).filter((k): k is RepositoryType => ['proxy', 'hosted', 'group'].includes(k)),
    cached: query.cached === '1' || query.cached === 'true',
  }
}

export function stateToQuery(state: SearchState): LocationQueryRaw {
  const out: LocationQueryRaw = {}
  if (state.q.trim()) out.q = state.q
  for (const facet of FACETS) if (state[facet].length) out[facet] = [...state[facet]]
  if (state.cached) out.cached = '1'
  return out
}

export function hasFilters(state: SearchState): boolean {
  return state.cached || FACETS.some((f) => state[f].length > 0)
}

/** Whether there is anything to search for: a query, or at least one filter (lists everything that matches). */
export function isActive(state: SearchState): boolean {
  return state.q.trim() !== '' || hasFilters(state)
}

export function toggle<T extends string>(values: readonly T[], value: T): T[] {
  return values.includes(value) ? values.filter((v) => v !== value) : [...values, value]
}

/** The repository Browse view showing a hit. */
export function browseLocation(hit: Pick<SearchHit, 'browse'>): RouteLocationRaw {
  return { name: 'repository', params: { name: hit.browse.repository, tab: 'browse' }, query: { ...hit.browse.query } }
}

export function searchLocation(q: string): RouteLocationRaw {
  return { name: 'search', query: q.trim() ? { q: q.trim() } : {} }
}

export function isDigest(q: string): boolean {
  return DIGEST_RE.test(q.trim())
}

/** The matched part of `name` for a name query (prefix of the name or of its last `/` segment), as segments. */
export function highlight(name: string, q: string): { text: string; match: boolean }[] {
  const term = q.trim().split(/\s+/)[0]?.toLowerCase() ?? ''
  if (!term || isDigest(q)) return [{ text: name, match: false }]
  const lower = name.toLowerCase()
  const at = lower.startsWith(term) ? 0 : lower.slice(lower.lastIndexOf('/') + 1).startsWith(term) ? lower.lastIndexOf('/') + 1 : -1
  if (at < 0) return [{ text: name, match: false }]
  return [
    { text: name.slice(0, at), match: false },
    { text: name.slice(at, at + term.length), match: true },
    { text: name.slice(at + term.length), match: false },
  ].filter((s) => s.text !== '')
}
