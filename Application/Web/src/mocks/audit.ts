// SPDX-License-Identifier: GPL-3.0-or-later
// Mock audit log (contract `/audit`, `/audit/export`): seeded records plus sign-ins made in mock mode, the same
// filters as the server, cursor paging and streamed-looking CSV/NDJSON attachments.
import { HttpResponse, http } from 'msw'

import type { AuditRecord } from '@/api/types'

import { auditCsv, auditNdjson, filterAudit, pageAudit, seedAudit } from './fixtures/audit'
import type { MockHelpers } from './tokens'

const API = '/api/v1'
const records: AuditRecord[] = seedAudit()
let seq = 0

/** Prepend a record (mock sign-in/out), newest first like the server. */
export function recordAudit(entry: Pick<AuditRecord, 'action' | 'outcome' | 'actor'> & Partial<AuditRecord>): void {
  records.unshift({
    id: `01JCAUDITLIVE${String(++seq).padStart(13, '0')}`,
    ts: new Date().toISOString(),
    target: null,
    client_ip: '192.0.2.14',
    request_id: null,
    reason: null,
    details: {},
    changes: [],
    ...entry,
  })
}

const invalid = (h: MockHelpers) => h.problem(422, 'Unprocessable Content', 'Invalid filter.', [{ field: 'from', message: 'Use an RFC 3339 time.', code: 'format' }])

export function auditHandlers(h: MockHelpers) {
  return [
    http.get(`${API}/audit`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const query = new URL(request.url).searchParams
      const matched = filterAudit(records, query)
      if (!matched) return invalid(h)
      return HttpResponse.json(pageAudit(matched, query))
    }),
    http.get(`${API}/audit/export`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const query = new URL(request.url).searchParams
      const matched = filterAudit(records, query)
      if (!matched) return invalid(h)
      const ndjson = query.get('format') === 'ndjson'
      const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
      return new HttpResponse(ndjson ? auditNdjson(matched) : auditCsv(matched), {
        headers: {
          'Content-Type': ndjson ? 'application/x-ndjson' : 'text/csv; charset=utf-8',
          'Content-Disposition': `attachment; filename="waystation-audit-${stamp}.${ndjson ? 'ndjson' : 'csv'}"`,
        },
      })
    }),
  ]
}
