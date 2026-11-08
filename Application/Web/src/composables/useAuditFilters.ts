// SPDX-License-Identifier: GPL-3.0-or-later
// Audit log filters live in the URL (shareable, survive reload, back button works): range (or custom from/to
// dates), actor, action (repeatable, `category.*` wildcards), target, outcome.
import { computed } from 'vue'
import { type LocationQuery, useRoute, useRouter } from 'vue-router'

import type { AuditOutcome } from '@/api/types'

export const AUDIT_RANGES = ['1h', '24h', '7d', '30d', 'all', 'custom'] as const
export type AuditRange = (typeof AUDIT_RANGES)[number]
export const DEFAULT_AUDIT_RANGE: AuditRange = '7d'

const RANGE_MS: Record<Exclude<AuditRange, 'all' | 'custom'>, number> = {
  '1h': 3_600_000,
  '24h': 86_400_000,
  '7d': 7 * 86_400_000,
  '30d': 30 * 86_400_000,
}

export interface AuditFilterState {
  range: AuditRange
  from: string | null // yyyy-mm-dd (custom range)
  to: string | null // yyyy-mm-dd, inclusive (custom range)
  actor: string | null
  actions: string[]
  target: string | null
  outcome: AuditOutcome | null
}

/** The `/audit` query parameters for the filters (`now` pins relative ranges). */
export interface AuditApiQuery {
  from?: string
  to?: string
  actor?: string
  action?: string[]
  target?: string
  outcome?: AuditOutcome
}

const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v.trim() : null)
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

export function parseAuditQuery(query: LocationQuery): AuditFilterState {
  const range = str(query.range)
  const raw = query.action
  const actions = (Array.isArray(raw) ? raw : [raw]).filter((a): a is string => typeof a === 'string' && a !== '')
  const outcome = str(query.outcome)
  const from = str(query.from)
  const to = str(query.to)
  return {
    range: (AUDIT_RANGES as readonly string[]).includes(range ?? '') ? (range as AuditRange) : DEFAULT_AUDIT_RANGE,
    from: from && DATE_RE.test(from) ? from : null,
    to: to && DATE_RE.test(to) ? to : null,
    actor: str(query.actor),
    actions,
    target: str(query.target),
    outcome: outcome === 'success' || outcome === 'failure' ? outcome : null,
  }
}

/** Local midnight of a yyyy-mm-dd date, as RFC 3339 UTC. */
function dayStart(date: string, plusDays = 0): string {
  const [y, m, d] = date.split('-').map(Number) as [number, number, number]
  return new Date(y, m - 1, d + plusDays).toISOString()
}

export function toApiQuery(state: AuditFilterState, now = Date.now()): AuditApiQuery {
  const q: AuditApiQuery = {}
  if (state.range === 'custom') {
    if (state.from) q.from = dayStart(state.from)
    if (state.to) q.to = dayStart(state.to, 1)
  } else if (state.range !== 'all') {
    q.from = new Date(now - RANGE_MS[state.range]).toISOString()
  }
  if (state.actor) q.actor = state.actor
  if (state.actions.length) q.action = state.actions
  if (state.target) q.target = state.target
  if (state.outcome) q.outcome = state.outcome
  return q
}

export function isFiltered(state: AuditFilterState): boolean {
  return Boolean(state.actor || state.actions.length || state.target || state.outcome || state.range !== DEFAULT_AUDIT_RANGE)
}

export function useAuditFilters() {
  const route = useRoute()
  const router = useRouter()
  const state = computed(() => parseAuditQuery(route.query))

  function update(changes: Partial<AuditFilterState>): void {
    const next = { ...state.value, ...changes }
    const query: Record<string, string | string[]> = {}
    if (next.range !== DEFAULT_AUDIT_RANGE) query.range = next.range
    if (next.range === 'custom') {
      if (next.from) query.from = next.from
      if (next.to) query.to = next.to
    }
    if (next.actor) query.actor = next.actor
    if (next.actions.length) query.action = next.actions
    if (next.target) query.target = next.target
    if (next.outcome) query.outcome = next.outcome
    void router.replace({ query })
  }

  function clear(): void {
    void router.replace({ query: {} })
  }

  return { state, update, clear }
}
