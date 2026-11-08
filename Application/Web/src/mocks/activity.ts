// SPDX-License-Identifier: GPL-3.0-or-later
// Mock `GET /activity` and `GET /activity/export` with the server's semantics: the same filters, stream-id cursors
// (exclusive, newest first), a CSV with formula-looking cells neutralised.
import type { ActivityEvent } from '@/api/types'
import { type ActivityQuery, compareIdsDesc, matchesQuery } from '@/utils/activity'

const KEYS = ['repo', 'kind', 'format', 'principal', 'cache', 'outcome', 'since', 'until'] as const

export function activityQuery(url: URL): ActivityQuery {
  const q: Record<string, string> = {}
  for (const key of KEYS) {
    const v = url.searchParams.get(key)
    if (v) q[key] = v
  }
  return q as ActivityQuery
}

export function activityPage(events: readonly ActivityEvent[], url: URL): { items: ActivityEvent[]; next_cursor: string | null } {
  const q = activityQuery(url)
  const limit = Math.min(500, Math.max(1, Number(url.searchParams.get('limit') ?? 50) || 50))
  const cursor = url.searchParams.get('cursor')
  const matching = events.filter((e) => (!cursor || compareIdsDesc(e.id, cursor) > 0) && matchesQuery(e, q))
  const items = matching.slice(0, limit)
  return { items, next_cursor: matching.length > limit ? items[items.length - 1]!.id : null }
}

const COLUMNS = ['time', 'kind', 'outcome', 'repository', 'format', 'artifact', 'path', 'principal', 'cache', 'bytes', 'duration_ms', 'summary', 'id'] as const

function cell(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') return String(value)
  let text = String(value)
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

export function activityCsv(events: readonly ActivityEvent[], url: URL, maxRows = 100_000): string {
  const q = activityQuery(url)
  const lines = [COLUMNS.join(',')]
  for (const e of events) {
    if (lines.length > maxRows) break
    if (!matchesQuery(e, q)) continue
    const row: Partial<Record<(typeof COLUMNS)[number], unknown>> = { ...e, time: e.ts }
    lines.push(COLUMNS.map((c) => cell(row[c])).join(','))
  }
  return `${lines.join('\r\n')}\r\n`
}
