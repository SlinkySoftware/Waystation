// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { parseAuditQuery, toApiQuery } from '@/composables/useAuditFilters'
import { ALL_AUDIT_ACTIONS, auditActionLabel } from '@/utils/audit'

import { auditCsv, auditNdjson, filterAudit, pageAudit, seedAudit } from './fixtures/audit'

const NOW = Date.parse('2026-11-09T12:00:00Z')
const records = seedAudit(NOW)
const q = (params: Record<string, string | string[]>) => {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) for (const x of [v].flat()) sp.append(k, x)
  return sp
}

describe('mock audit log', () => {
  it('seeds ~150 records over 30 days, newest first, covering every catalog action with a label', () => {
    expect(records.length).toBe(150)
    expect(records.map((r) => r.ts)).toEqual([...records.map((r) => r.ts)].sort().reverse())
    expect(Date.parse(records.at(-1)!.ts)).toBeGreaterThanOrEqual(NOW - 30 * 86_400_000)
    const seen = new Set(records.map((r) => r.action))
    for (const action of ALL_AUDIT_ACTIONS) {
      expect(seen.has(action), action).toBe(true)
      expect(auditActionLabel(action)).not.toBe(action)
    }
    expect(records.some((r) => r.outcome === 'failure' && r.reason)).toBe(true)
    expect(records.some((r) => r.changes.some((c) => c.redacted))).toBe(true)
  })

  it('filters by action (exact and category wildcard), actor, target, outcome and time', () => {
    expect(filterAudit(records, q({ action: 'token.create' }))!.every((r) => r.action === 'token.create')).toBe(true)
    const tokenish = filterAudit(records, q({ action: ['token.*', 'auth.logout'] }))!
    expect(tokenish.length).toBeGreaterThan(0)
    expect(tokenish.every((r) => r.action.startsWith('token.') || r.action === 'auth.logout')).toBe(true)
    expect(filterAudit(records, q({ actor: 'ADMIN' }))!.every((r) => r.actor.name === 'admin')).toBe(true)
    expect(filterAudit(records, q({ target: 'ci-runner' }))!.every((r) => r.target?.name === 'ci-runner')).toBe(true)
    expect(filterAudit(records, q({ outcome: 'failure' }))!.every((r) => r.outcome === 'failure')).toBe(true)
    const from = new Date(NOW - 86_400_000).toISOString()
    expect(filterAudit(records, q({ from }))!.every((r) => r.ts >= from)).toBe(true)
    expect(filterAudit(records, q({ from: 'yesterday' }))).toBeNull()
    expect(filterAudit(records, q({ outcome: 'maybe' }))).toBeNull()
  })

  it('pages with an opaque cursor and caps the limit at 200', () => {
    const first = pageAudit(records, q({ limit: '60' }))
    expect(first.items).toHaveLength(60)
    const second = pageAudit(records, q({ limit: '60', cursor: first.next_cursor! }))
    expect(second.items[0]).toEqual(records[60])
    expect(pageAudit(records, q({ limit: '1000' })).items).toHaveLength(150)
    expect(pageAudit(records, q({ limit: '200' })).next_cursor).toBeNull()
  })

  it('exports CSV with a header and formula-safe cells, NDJSON with one record per line', () => {
    const csv = auditCsv([{ ...records[0]!, details: {}, target: { kind: 'user', id: null, name: '=cmd()' } }])
    expect(csv.split('\r\n')[0]).toMatch(/^ts,action,outcome/)
    expect(csv).toContain("'=cmd()")
    const lines = auditNdjson(records.slice(0, 3)).trim().split('\n')
    expect(lines.map((l) => (JSON.parse(l) as { id: string }).id)).toEqual(records.slice(0, 3).map((r) => r.id))
  })
})

describe('audit filters in the URL', () => {
  it('parses the query and turns relative ranges into a from time', () => {
    const state = parseAuditQuery({ range: '24h', action: ['token.*', 'auth.login'], outcome: 'failure', actor: 'admin' })
    expect(state.actions).toEqual(['token.*', 'auth.login'])
    expect(toApiQuery(state, NOW)).toEqual({ from: new Date(NOW - 86_400_000).toISOString(), actor: 'admin', action: ['token.*', 'auth.login'], outcome: 'failure' })
    expect(parseAuditQuery({ range: 'bogus', outcome: 'nope' })).toMatchObject({ range: '7d', outcome: null })
    expect(toApiQuery(parseAuditQuery({ range: 'all' }), NOW)).toEqual({})
  })
})
