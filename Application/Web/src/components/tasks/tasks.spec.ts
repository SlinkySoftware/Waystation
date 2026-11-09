// SPDX-License-Identifier: GPL-3.0-or-later
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it } from 'vitest'

import type { JobRun, JobRunSummary, TaskEvent } from '@/api/types'
import { useLiveStore } from '@/stores/live'

import { appendLog, applyRunEvent, describeTaskSchedule, heartbeatAge, kindLabel, queueLabel, resultEntries, scheduleError, taskLabel, workerStateMeta } from './tasks'

function run(partial: Partial<JobRunSummary> = {}): JobRunSummary {
  return {
    id: 'r1',
    job_id: 'j1',
    kind: 'blob.gc',
    queue: 'maintenance',
    task_id: 'blob.gc',
    trigger: 'schedule',
    attempt: 1,
    status: 'running',
    started_at: '2026-11-09T10:00:00Z',
    finished_at: null,
    duration_ms: null,
    progress: { done: 0, total: 10, message: null },
    worker_id: 'w1',
    error: null,
    result: {},
    ...partial,
  }
}

describe('task labels', () => {
  it('names known kinds and passes unknown ones through', () => {
    expect(kindLabel('blob.gc')).toBe('Blob garbage collection')
    expect(kindLabel('index.apt')).toBe('index.apt')
    expect(taskLabel({ kind: 'cleanup.run', owner: { kind: 'cleanup_policy', id: 'p1', name: 'npm-unused-90d' } })).toBe('Policy npm-unused-90d')
    expect(queueLabel('maintenance')).toBe('Maintenance')
    expect(queueLabel('custom')).toBe('custom')
  })

  it('reads schedules in words, including @every', () => {
    expect(describeTaskSchedule('@every 10s')).toBe('Every 10 seconds')
    expect(describeTaskSchedule('@every 5m')).toBe('Every 5 minutes')
    expect(describeTaskSchedule('0 3 * * *')).toBe('Daily at 03:00 UTC')
  })
})

describe('schedule validation (client side)', () => {
  it.each(['@every 10s', '@every 1h', '@daily', '0 */6 * * *', '30 2 * * MON-FRI', '  0  3 * * * '])('accepts %s', (s) => {
    expect(scheduleError(s)).toBeNull()
  })

  it.each([
    ['', 'Enter a schedule'],
    ['@every 5s', 'Use at least @every 10s'],
    ['@sometimes', null],
    ['* * * *', null],
    ['0 3 * * * *', null],
  ])('rejects %j', (s, message) => {
    const error = scheduleError(s)
    expect(error).not.toBeNull()
    if (message) expect(error).toBe(message)
  })
})

describe('live run updates', () => {
  it('moves progress and status in place and reports unknown runs', () => {
    const rows = [run()]
    expect(applyRunEvent(rows, { type: 'job.progress', ts: '2026-11-09T10:00:02Z', run_id: 'r1', job_id: 'j1', kind: 'blob.gc', done: 4, total: 10, message: 'sweeping' })).toBe(true)
    expect(rows[0]!.progress).toEqual({ done: 4, total: 10, message: 'sweeping' })
    applyRunEvent(rows, { type: 'job.update', ts: '2026-11-09T10:00:05Z', event: 'failed', run_id: 'r1', job_id: 'j1', kind: 'blob.gc', queue: 'maintenance', attempt: 1, status: 'dead', error: 'disk full' })
    expect(rows[0]).toMatchObject({ status: 'dead', error: 'disk full', finished_at: '2026-11-09T10:00:05Z', duration_ms: 5000 })
    // progress after the end is ignored; a run not in the list asks the caller to refetch
    applyRunEvent(rows, { type: 'job.progress', ts: '2026-11-09T10:00:06Z', run_id: 'r1', job_id: 'j1', kind: 'blob.gc', done: 9, total: 10 })
    expect(rows[0]!.progress.done).toBe(4)
    expect(applyRunEvent(rows, { type: 'job.update', ts: '2026-11-09T10:00:06Z', event: 'started', run_id: 'r2', job_id: 'j2', kind: 'blob.gc', queue: 'maintenance', attempt: 1, status: 'running' })).toBe(false)
  })

  it('appends live log lines to the open run only, keeping the last 500', () => {
    const open: JobRun = { ...run(), log: Array.from({ length: 499 }, (_, i) => `line ${i}`) }
    appendLog(open, { type: 'job.log', ts: 'x', run_id: 'r1', job_id: 'j1', kind: 'blob.gc', lines: ['a', 'b'] })
    expect(open.log).toHaveLength(500)
    expect(open.log.slice(-2)).toEqual(['a', 'b'])
    appendLog(open, { type: 'job.log', ts: 'x', run_id: 'other', job_id: 'j9', kind: 'blob.gc', lines: ['c'] })
    expect(open.log.at(-1)).toBe('b')
  })

  it('routes tasks-topic events to listeners', () => {
    setActivePinia(createPinia())
    const live = useLiveStore()
    const seen: TaskEvent[] = []
    const off = live.onTasks((event) => seen.push(event))
    live.apply({ type: 'worker.update', ts: 'x', worker_id: 'w1', state: 'offline' })
    live.apply({ type: 'stats.tick', ts: 'x', requests_per_s: 0, bytes_out_per_s: 0, bytes_upstream_per_s: 0, hit_ratio_1m: null, active_transfers: 0 })
    off()
    live.apply({ type: 'worker.update', ts: 'x', worker_id: 'w2', state: 'online' })
    expect(seen).toEqual([{ type: 'worker.update', ts: 'x', worker_id: 'w1', state: 'offline' }])
  })
})

describe('workers and results', () => {
  it('describes worker health and counts heartbeat age on', () => {
    expect(workerStateMeta('ok').tone).toBe('tone-ok')
    expect(workerStateMeta('late').label).toBe('Late')
    expect(workerStateMeta('lost').tone).toBe('tone-blocked')
    expect(heartbeatAge({ heartbeat_age_s: 4 }, 1000, 7000)).toBe(10)
  })

  it('lists result figures as text', () => {
    expect(resultEntries({ freed_blobs: 3, repositories: ['npm'] })).toEqual([
      { key: 'freed blobs', value: '3' },
      { key: 'repositories', value: '["npm"]' },
    ])
  })
})
