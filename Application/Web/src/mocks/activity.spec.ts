// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { ActivityEvent } from '@/api/types'

import { activityCsv, activityPage } from './activity'

const events: ActivityEvent[] = Array.from({ length: 7 }, (_, i) => {
  const ms = 1_000_000 - i * 1000
  return {
    id: `${ms}-0`,
    ts: new Date(ms).toISOString(),
    kind: i === 3 ? 'error' : 'download',
    summary: i === 3 ? '=1+1, "boom"' : `event ${i}`,
    outcome: i === 3 ? 'failure' : 'success',
    repository: 'npm',
    format: 'npm',
    principal: i % 2 ? 'alice' : 'bob',
    cache: 'HIT',
    bytes: 10,
  }
})
const url = (q: string) => new URL(`http://localhost/api/v1/activity?${q}`)

describe('mock activity paging', () => {
  it('pages newest first with exclusive stream-id cursors', () => {
    const first = activityPage(events, url('limit=3'))
    expect(first.items.map((e) => e.id)).toEqual(['1000000-0', '999000-0', '998000-0'])
    const second = activityPage(events, url(`limit=3&cursor=${first.next_cursor}`))
    expect(second.items.map((e) => e.id)).toEqual(['997000-0', '996000-0', '995000-0'])
    const last = activityPage(events, url(`limit=3&cursor=${second.next_cursor}`))
    expect(last.items.map((e) => e.id)).toEqual(['994000-0'])
    expect(last.next_cursor).toBeNull()
  })

  it('applies the filters', () => {
    expect(activityPage(events, url('principal=ALICE')).items).toHaveLength(3)
    expect(activityPage(events, url('outcome=failure')).items.map((e) => e.id)).toEqual(['997000-0'])
  })
})

describe('mock CSV export', () => {
  it('writes a header, quotes and neutralises formulas', () => {
    const lines = activityCsv(events, url('kind=error')).trimEnd().split('\r\n')
    expect(lines[0]).toBe('time,kind,outcome,repository,format,artifact,path,principal,cache,bytes,duration_ms,summary,id')
    expect(lines).toHaveLength(2)
    expect(lines[1]).toContain('"\'=1+1, ""boom"""')
  })
})
