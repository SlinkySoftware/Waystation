// SPDX-License-Identifier: GPL-3.0-or-later
// Activity page logic (UI_DESIGN §4.6): filters <-> URL, filters -> API query, the same filter applied to live events,
// merging live events into the loaded log, and display metadata per event kind.
import type { LocationQuery, LocationQueryRaw } from 'vue-router'

import type { ActivityEvent, ActivityKind, CacheState, Format } from '@/api/types'
import { t } from '@/i18n'

import {
  iconCleanup,
  iconDelete,
  iconError,
  iconFetching,
  iconHit,
  iconInfo,
  iconLock,
  iconRepositories,
  iconStorage,
  iconUpload,
  iconWarning,
} from './icons'

export type ActivityOutcome = ActivityEvent['outcome']

export const ACTIVITY_KINDS: readonly ActivityKind[] = ['download', 'cache_miss', 'upload', 'delete', 'error', 'upstream_state', 'repository', 'cleanup', 'storage', 'auth']
export const ACTIVITY_OUTCOMES: readonly ActivityOutcome[] = ['success', 'failure', 'info']
export const ACTIVITY_CACHE_STATES: readonly CacheState[] = ['HIT', 'FETCHING', 'COALESCED', 'STALE', 'REVALIDATED', 'ERROR']
const FORMAT_RE = /^[a-z]{2,16}$/
const REPO_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$/

/** Relative windows ending now; `null` = all retained history. */
export const ACTIVITY_RANGES = { '15m': 15 * 60_000, '1h': 3_600_000, '24h': 86_400_000, '7d': 7 * 86_400_000 } as const
export type ActivityRange = keyof typeof ACTIVITY_RANGES

export interface ActivityFilters {
  kind: ActivityKind | null
  repo: string | null
  format: Format | null
  principal: string | null
  cache: CacheState | null
  outcome: ActivityOutcome | null
  range: ActivityRange | null
  /** Absolute window (custom range), ISO timestamps. Ignored when `range` is set. */
  from: string | null
  to: string | null
}

export const NO_FILTERS: ActivityFilters = { kind: null, repo: null, format: null, principal: null, cache: null, outcome: null, range: null, from: null, to: null }

/** The API's query parameters for `GET /activity` and `GET /activity/export`. */
export interface ActivityQuery {
  repo?: string
  kind?: ActivityKind
  format?: Format
  principal?: string
  cache?: CacheState
  outcome?: ActivityOutcome
  since?: string
  until?: string
}

const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v.trim() : null)
const oneOf = <T extends string>(v: unknown, allowed: readonly T[]): T | null => {
  const s = str(v)
  return s !== null && (allowed as readonly string[]).includes(s) ? (s as T) : null
}
const isoOrNull = (v: unknown): string | null => {
  const s = str(v)
  return s !== null && !Number.isNaN(Date.parse(s)) ? new Date(s).toISOString() : null
}

/** Filters from the route query (URL keys: kind, repo, format, user, cache, outcome, range, from, to); junk is dropped. */
export function filtersFromQuery(query: LocationQuery): ActivityFilters {
  const repo = str(query.repo)
  const format = str(query.format)
  const range = oneOf(query.range, Object.keys(ACTIVITY_RANGES) as ActivityRange[])
  return {
    kind: oneOf(query.kind, ACTIVITY_KINDS),
    repo: repo && REPO_RE.test(repo) ? repo : null,
    format: format && FORMAT_RE.test(format) ? (format as Format) : null,
    principal: str(query.user)?.slice(0, 128) ?? null,
    cache: oneOf(query.cache, ACTIVITY_CACHE_STATES),
    outcome: oneOf(query.outcome, ACTIVITY_OUTCOMES),
    range,
    from: range ? null : isoOrNull(query.from),
    to: range ? null : isoOrNull(query.to),
  }
}

export function filtersToQuery(f: ActivityFilters): LocationQueryRaw {
  const out: Record<string, string> = {}
  const set = (key: string, value: string | null) => {
    if (value) out[key] = value
  }
  set('kind', f.kind)
  set('repo', f.repo)
  set('format', f.format)
  set('user', f.principal)
  set('cache', f.cache)
  set('outcome', f.outcome)
  set('range', f.range)
  if (!f.range) {
    set('from', f.from)
    set('to', f.to)
  }
  return out
}

/** Number of active filters (for the "clear" affordance). */
export function activeFilterCount(f: ActivityFilters): number {
  return [f.kind, f.repo, f.format, f.principal, f.cache, f.outcome, f.range ?? (f.from || f.to ? 'custom' : null)].filter(Boolean).length
}

/** The time window in epoch ms (either end may be open). */
export function timeBounds(f: ActivityFilters, now = Date.now()): { since: number | null; until: number | null } {
  if (f.range) return { since: now - ACTIVITY_RANGES[f.range], until: null }
  return { since: f.from ? Date.parse(f.from) : null, until: f.to ? Date.parse(f.to) : null }
}

export function toApiQuery(f: ActivityFilters, now = Date.now()): ActivityQuery {
  const { since, until } = timeBounds(f, now)
  const q: ActivityQuery = {}
  if (f.repo) q.repo = f.repo
  if (f.kind) q.kind = f.kind
  if (f.format) q.format = f.format
  if (f.principal) q.principal = f.principal
  if (f.cache) q.cache = f.cache
  if (f.outcome) q.outcome = f.outcome
  if (since !== null) q.since = new Date(since).toISOString()
  if (until !== null) q.until = new Date(until).toISOString()
  return q
}

/** Server semantics of the activity filters (live events are filtered client-side the same way; the mocks use it too). */
export function matchesQuery(e: ActivityEvent, q: ActivityQuery): boolean {
  if (q.repo && e.repository !== q.repo) return false
  if (q.kind && e.kind !== q.kind) return false
  if (q.format && e.format !== q.format) return false
  if (q.cache && e.cache !== q.cache) return false
  if (q.outcome && e.outcome !== q.outcome) return false
  if (q.principal && !(e.principal ?? '').toLowerCase().includes(q.principal.toLowerCase())) return false
  const ts = Date.parse(e.ts)
  if (q.since && ts < Date.parse(q.since)) return false
  if (q.until && ts > Date.parse(q.until)) return false
  return true
}

/** Live events can only appear when the window is open-ended (no absolute end in the past). */
export function canFollow(f: ActivityFilters, now = Date.now()): boolean {
  return f.range !== null || !f.to || Date.parse(f.to) > now
}

/** Stream ids (`<ms>-<seq>`) in time order; newer sorts first. */
export function compareIdsDesc(a: string, b: string): number {
  const [am = 0, as = 0] = a.split('-').map(Number)
  const [bm = 0, bs = 0] = b.split('-').map(Number)
  return bm - am || bs - as
}

/** Union by id, newest first. `incoming` may overlap `current` (resume, reconnect replay, page/live race). */
export function mergeEvents(current: readonly ActivityEvent[], incoming: readonly ActivityEvent[]): ActivityEvent[] {
  if (!incoming.length) return current.slice()
  const seen = new Set(current.map((e) => e.id))
  const fresh = incoming.filter((e) => !seen.has(e.id) && (seen.add(e.id), true))
  if (!fresh.length) return current.slice()
  return [...fresh, ...current].sort((a, b) => compareIdsDesc(a.id, b.id))
}

export interface KindMeta {
  label: string
  icon: string
}

export function kindMeta(kind: ActivityKind): KindMeta {
  switch (kind) {
    case 'download':
      return { label: t('activity.kind.download'), icon: iconHit }
    case 'cache_miss':
      return { label: t('activity.kind.cache_miss'), icon: iconFetching }
    case 'upload':
      return { label: t('activity.kind.upload'), icon: iconUpload }
    case 'delete':
      return { label: t('activity.kind.delete'), icon: iconDelete }
    case 'error':
      return { label: t('activity.kind.error'), icon: iconError }
    case 'upstream_state':
      return { label: t('activity.kind.upstream_state'), icon: iconWarning }
    case 'repository':
      return { label: t('activity.kind.repository'), icon: iconRepositories }
    case 'cleanup':
      return { label: t('activity.kind.cleanup'), icon: iconCleanup }
    case 'storage':
      return { label: t('activity.kind.storage'), icon: iconStorage }
    case 'auth':
      return { label: t('activity.kind.auth'), icon: iconLock }
    default:
      return { label: kind, icon: iconInfo }
  }
}

/** Colour class for the kind icon (never the only signal: the icon and label carry the meaning). */
export function activityTone(e: Pick<ActivityEvent, 'kind' | 'outcome'>): string {
  if (e.outcome === 'failure') return 'tone-error'
  if (e.kind === 'cache_miss') return 'tone-miss'
  if (e.kind === 'download') return 'tone-hit'
  if (e.kind === 'upload') return 'tone-ok'
  return 'tone-offline'
}

/** The file name from a `Content-Disposition` header, or `fallback`. */
export function filenameFrom(disposition: string | null, fallback: string): string {
  const match = disposition?.match(/filename="?([^";]+)"?/i)
  const name = match?.[1]?.trim()
  return name && /^[\w.-]+$/.test(name) ? name : fallback
}
