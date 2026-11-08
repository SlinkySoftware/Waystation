// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { ActivityEvent } from '@/api/types'

import {
  activeFilterCount,
  canFollow,
  compareIdsDesc,
  filenameFrom,
  filtersFromQuery,
  filtersToQuery,
  matchesQuery,
  mergeEvents,
  NO_FILTERS,
  timeBounds,
  toApiQuery,
} from './activity'

const NOW = Date.parse('2026-11-09T12:00:00Z')

function ev(id: string, extra: Partial<ActivityEvent> = {}): ActivityEvent {
  const ms = Number(id.split('-')[0])
  return { id, ts: new Date(ms).toISOString(), kind: 'download', summary: 's', outcome: 'success', repository: 'npm', format: 'npm', principal: 'alice', cache: 'HIT', ...extra }
}

describe('filters <-> URL', () => {
  it('round-trips every filter', () => {
    const f = { ...NO_FILTERS, kind: 'error' as const, repo: 'dockerhub-proxy', format: 'docker' as const, principal: 'ci-runner', cache: 'STALE' as const, outcome: 'failure' as const, range: '1h' as const }
    const query = filtersToQuery(f)
    expect(query).toEqual({ kind: 'error', repo: 'dockerhub-proxy', format: 'docker', user: 'ci-runner', cache: 'STALE', outcome: 'failure', range: '1h' })
    expect(filtersFromQuery(query as Record<string, string>)).toEqual(f)
  })

  it('drops junk and keeps a custom window only without a preset', () => {
    expect(filtersFromQuery({ kind: 'nope', repo: '../etc', cache: 'hit', range: '99y', from: 'garbage' })).toEqual(NO_FILTERS)
    const custom = filtersFromQuery({ from: '2026-11-09T10:00:00Z', to: '2026-11-09T11:00:00.000Z' })
    expect(custom.from).toBe('2026-11-09T10:00:00.000Z')
    expect(custom.to).toBe('2026-11-09T11:00:00.000Z')
    expect(filtersFromQuery({ range: '15m', from: '2026-11-09T10:00:00Z' }).from).toBeNull()
  })

  it('counts active filters, a custom window as one', () => {
    expect(activeFilterCount(NO_FILTERS)).toBe(0)
    expect(activeFilterCount({ ...NO_FILTERS, repo: 'npm', from: '2026-11-09T10:00:00.000Z', to: '2026-11-09T11:00:00.000Z' })).toBe(2)
  })
})

describe('time window', () => {
  it('turns a preset into an open-ended window ending now', () => {
    expect(timeBounds({ ...NO_FILTERS, range: '15m' }, NOW)).toEqual({ since: NOW - 15 * 60_000, until: null })
    expect(toApiQuery({ ...NO_FILTERS, range: '1h', principal: 'bob' }, NOW)).toEqual({ principal: 'bob', since: '2026-11-09T11:00:00.000Z' })
  })

  it('follows only when the window has no end in the past', () => {
    expect(canFollow(NO_FILTERS, NOW)).toBe(true)
    expect(canFollow({ ...NO_FILTERS, range: '24h' }, NOW)).toBe(true)
    expect(canFollow({ ...NO_FILTERS, to: '2026-11-09T11:00:00.000Z' }, NOW)).toBe(false)
    expect(canFollow({ ...NO_FILTERS, from: '2026-11-09T11:00:00.000Z' }, NOW)).toBe(true)
  })
})

describe('matchesQuery (server semantics for live events)', () => {
  const e = ev(`${NOW}-0`)
  it('matches exact fields and a case-insensitive principal substring', () => {
    expect(matchesQuery(e, {})).toBe(true)
    expect(matchesQuery(e, { repo: 'npm', kind: 'download', format: 'npm', cache: 'HIT', outcome: 'success', principal: 'ALI' })).toBe(true)
    expect(matchesQuery(e, { repo: 'npmjs' })).toBe(false)
    expect(matchesQuery(e, { cache: 'FETCHING' })).toBe(false)
    expect(matchesQuery(e, { outcome: 'failure' })).toBe(false)
    expect(matchesQuery(e, { principal: 'bob' })).toBe(false)
    expect(matchesQuery({ ...e, principal: null }, { principal: 'a' })).toBe(false)
  })

  it('bounds by time inclusively', () => {
    expect(matchesQuery(e, { since: new Date(NOW).toISOString(), until: new Date(NOW).toISOString() })).toBe(true)
    expect(matchesQuery(e, { since: new Date(NOW + 1).toISOString() })).toBe(false)
    expect(matchesQuery(e, { until: new Date(NOW - 1).toISOString() })).toBe(false)
  })
})

describe('merging the live tail into the log', () => {
  it('orders stream ids numerically, newest first', () => {
    expect(['999-0', '1000-2', '1000-10'].sort(compareIdsDesc)).toEqual(['1000-10', '1000-2', '999-0'])
  })

  it('dedupes by id and keeps newest first', () => {
    const current = [ev('3000-0'), ev('2000-0')]
    const merged = mergeEvents(current, [ev('2000-0'), ev('4000-0'), ev('2500-1'), ev('4000-0')])
    expect(merged.map((x) => x.id)).toEqual(['4000-0', '3000-0', '2500-1', '2000-0'])
    expect(mergeEvents(current, [ev('3000-0')])).toEqual(current)
  })
})

describe('filenameFrom', () => {
  it('takes a safe file name from Content-Disposition', () => {
    expect(filenameFrom('attachment; filename="waystation-activity-20261109-120000.csv"', 'x.csv')).toBe('waystation-activity-20261109-120000.csv')
    expect(filenameFrom('attachment; filename="../../evil"', 'x.csv')).toBe('x.csv')
    expect(filenameFrom(null, 'x.csv')).toBe('x.csv')
  })
})
