// SPDX-License-Identifier: GPL-3.0-or-later
import { getResponse } from 'msw'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import type { DeadLetterPage, JobAccepted, JobRun, JobRunPage, Problem, ScheduledTask, ScheduledTaskList, TaskEvent, WorkerOverview } from '@/api/types'

import { handlers } from './handlers'
import { subscribeTasks, taskTick } from './tasks'

const CSRF = 'mock-csrf-7f3a9c'
async function api<T>(method: string, path: string, body?: unknown): Promise<{ status: number; json: T }> {
  const request = new Request(new URL(`/api/v1${path}`, location.origin), {
    method,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const response = (await getResponse(handlers, request))!
  return { status: response.status, json: response.status === 204 ? (null as T) : ((await response.json()) as T) }
}

describe('mock tasks API', () => {
  beforeAll(() => {
    localStorage.setItem('ws.mock.session', 'admin')
    vi.useFakeTimers({ toFake: ['Date'] })
  })
  afterAll(() => {
    vi.useRealTimers()
    localStorage.clear()
  })

  it('lists built-in and policy tasks; a policy owns its schedule', async () => {
    const { json } = await api<ScheduledTaskList>('GET', '/tasks/scheduled')
    const byId = new Map(json.items.map((task) => [task.id, task]))
    expect(byId.get('stats.rollup')).toMatchObject({ routine: true, owner: null, schedule: '@every 10s' })
    expect(byId.get('blob.gc')?.last_run?.kind).toBe('blob.gc')
    const policy = json.items.find((task) => task.owner)!
    expect(policy.kind).toBe('cleanup.run')
    const refused = await api<Problem>('PATCH', `/tasks/scheduled/${policy.id}`, { schedule: '@daily' })
    expect(refused.status).toBe(409)
  })

  it('validates and saves a schedule, and pauses a task', async () => {
    const bad = await api<Problem>('PATCH', '/tasks/scheduled/blob.gc', { schedule: '@every 2s' })
    expect(bad.status).toBe(422)
    expect(bad.json.errors?.[0]?.field).toBe('schedule')
    expect((await api<Problem>('PATCH', '/tasks/scheduled/blob.gc', { schedule: '* * *' })).status).toBe(422)
    const saved = await api<ScheduledTask>('PATCH', '/tasks/scheduled/blob.gc', { schedule: '  30 4 * * *  ' })
    expect(saved.json.schedule).toBe('30 4 * * *')
    const paused = await api<ScheduledTask>('PATCH', '/tasks/scheduled/blob.gc', { enabled: false })
    expect(paused.json).toMatchObject({ enabled: false, next_run_at: null })
    await api('PATCH', '/tasks/scheduled/blob.gc', { enabled: true })
    expect((await api('PATCH', '/tasks/scheduled/nope', { enabled: true })).status).toBe(404)
  })

  it('runs a task now and merges a second request into the running one', async () => {
    const first = await api<JobAccepted>('POST', '/tasks/scheduled/blob.gc/run')
    expect(first.status).toBe(202)
    expect(first.json.status).toBe('enqueued')
    const again = await api<JobAccepted>('POST', '/tasks/scheduled/blob.gc/run')
    expect(again.json).toEqual({ job_id: first.json.job_id, status: 'merged' })
    const runs = await api<JobRunPage>('GET', '/tasks/runs?status=running&kind=blob.gc')
    expect(runs.json.items.map((r) => r.id)).toContain(first.json.job_id)
    const detail = await api<JobRun>('GET', `/tasks/runs/${first.json.job_id}`)
    expect(detail.json.trigger).toBe('manual')
    expect(detail.json.log.length).toBeGreaterThan(0)
  })

  it('pages runs newest first and hides routine runs unless asked', async () => {
    const first = await api<JobRunPage>('GET', '/tasks/runs?limit=5')
    expect(first.json.items).toHaveLength(5)
    expect(first.json.items.every((r) => r.kind !== 'stats.rollup')).toBe(true)
    const starts = first.json.items.map((r) => r.started_at)
    expect([...starts].sort().reverse()).toEqual(starts)
    const second = await api<JobRunPage>('GET', `/tasks/runs?limit=5&cursor=${first.json.next_cursor}`)
    expect(second.json.items[0]!.id).not.toBe(first.json.items[0]!.id)
    const routine = await api<JobRunPage>('GET', '/tasks/runs?include_routine=true&kind=stats.rollup&limit=3')
    expect(routine.json.items).toHaveLength(3)
  })

  it('shows workers with their running jobs and the queues', async () => {
    const { json } = await api<WorkerOverview>('GET', '/tasks/workers')
    expect(json.workers.filter((w) => w.leader)).toHaveLength(1)
    expect(json.queues.map((q) => q.name)).toEqual(['index', 'maintenance', 'stats', 'prefetch', 'import'])
    expect(json.dead_letters).toBeGreaterThan(0)
  })

  it('retries and discards dead letters; malformed ones can only be discarded', async () => {
    const { json } = await api<DeadLetterPage>('GET', '/tasks/dead-letters')
    const retryable = json.items.find((d) => d.retryable)!
    const malformed = json.items.find((d) => !d.retryable)!
    const retried = await api<JobAccepted>('POST', `/tasks/dead-letters/${retryable.id}/retry`)
    expect(retried.status).toBe(202)
    expect((await api('POST', `/tasks/dead-letters/${retryable.id}/retry`)).status).toBe(404)
    expect((await api('POST', `/tasks/dead-letters/${malformed.id}/retry`)).status).toBe(409)
    expect((await api('DELETE', `/tasks/dead-letters/${malformed.id}`)).status).toBe(204)
    expect((await api('DELETE', `/tasks/dead-letters/${malformed.id}`)).status).toBe(204)
    const after = await api<DeadLetterPage>('GET', '/tasks/dead-letters')
    expect(after.json.total).toBe(json.total - 2)
    const runs = await api<JobRunPage>('GET', '/tasks/runs?status=running')
    expect(runs.json.items.some((r) => r.trigger === 'retry' && r.job_id === retried.json.job_id)).toBe(true)
  })

  it('emits live events as runs start, progress and finish', async () => {
    const events: TaskEvent[] = []
    const off = subscribeTasks((event) => events.push(event))
    try {
      const started = await api<JobAccepted>('POST', '/tasks/scheduled/stats.rollup/run')
      expect(started.status).toBe(202)
      const gc = await api<JobAccepted>('POST', '/tasks/scheduled/blob.gc/run')
      taskTick()
      vi.setSystemTime(Date.now() + 4000)
      taskTick()
      vi.setSystemTime(Date.now() + 30_000)
      taskTick()
      const forRun = events.filter((e) => 'run_id' in e && e.run_id === gc.json.job_id)
      expect(forRun.some((e) => e.type === 'job.progress')).toBe(true)
      expect(forRun.some((e) => e.type === 'job.log')).toBe(true)
      expect(forRun.some((e) => e.type === 'job.update' && e.event === 'finished' && e.status === 'succeeded')).toBe(true)
    } finally {
      off()
    }
  })
})
