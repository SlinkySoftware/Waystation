// SPDX-License-Identifier: GPL-3.0-or-later
// Mock tasks and workers (contract tag `tasks`): scheduled tasks over the cleanup mock's policies and blob GC plus
// the 10 s stats rollup, a run list merged from those, two workers, queue figures and dead letters. While the
// Tasks page follows the `tasks` topic, a 1 s timer advances runs and emits job.update / job.progress / job.log /
// worker.update like the server, and now and then a schedule "fires" so the page stays alive.
import { http, HttpResponse } from 'msw'

import type { AuditActor, CleanupRunSummary, DeadLetter, GcRun, JobAccepted, JobRun, JobRunSummary, QueueFigures, RunStatus, ScheduledTask, ScheduledTaskUpdate, TaskEvent, Worker } from '@/api/types'

import { recordAudit } from './audit'
import { cleanupTasks, gcTask, startTaskRun } from './cleanup'
import { between, GiB, hash, MiB, rng } from './rng'
import type { MockHelpers } from './tokens'
import { world } from './world'

const API = '/api/v1'
const ADMIN: AuditActor = { kind: 'user', id: '01JC0000000000000000000ADM', name: 'admin', auth: 'session', provider: 'local', token_id: null }
const iso = (ms: number) => new Date(ms).toISOString()
const MINUTE = 60_000
const ROLLUP_KEEP = 60

type Listener = (event: TaskEvent) => void
const listeners = new Set<Listener>()
let timer: ReturnType<typeof setInterval> | null = null
let nextFire = Date.now() + 25_000
/** Last status / progress / log length seen per run, to turn state changes into live events. */
const seen = new Map<string, { status: RunStatus; done: number; lines: number }>()
const startedAt = Date.now()
const rollup = { schedule: '@every 10s', enabled: true }

const WORKERS: Worker[] = [
  { id: 'ws-worker-1-48213-k2m9qd', hostname: 'ws-worker-1', version: '0.1.0', queues: ['index', 'maintenance', 'stats', 'prefetch', 'import'], concurrency: 4, leader: true, started_at: iso(startedAt - 3 * 86_400_000 - 4 * 3_600_000), heartbeat_at: iso(startedAt), heartbeat_age_s: 0, state: 'ok', running_jobs: [] },
  { id: 'ws-worker-2-51877-p8x1rt', hostname: 'ws-worker-2', version: '0.1.0', queues: ['index', 'maintenance', 'prefetch'], concurrency: 2, leader: false, started_at: iso(startedAt - 26 * 3_600_000), heartbeat_at: iso(startedAt), heartbeat_age_s: 0, state: 'ok', running_jobs: [] },
]

function deadEnvelope(kind: string, payload: Record<string, unknown>, attempts: number, minutesAgo: number, error: string): DeadLetter {
  const at = startedAt - minutesAgo * MINUTE
  return { id: `${at}-0`, job_id: `01JCDEAD${String(at).slice(-10)}${kind.length}`, kind, queue: kind === 'stats.rollup' ? 'stats' : 'maintenance', task_id: null, attempts, error, dead_at: iso(at), enqueued_at: iso(at - attempts * 4 * MINUTE), payload, retryable: true }
}

const deadLetters: DeadLetter[] = [
  deadEnvelope('blob.gc', { grace_s: 86400, upload_age_s: 86400 }, 3, 95, "OSError: [Errno 28] No space left on device: '/data/blobs/.gc-journal'"),
  deadEnvelope('cleanup.run', { policy_id: 'cp_0002dockerkeepl' }, 3, 26 * 60, 'ServerSelectionTimeoutError: replica set rs0 has no primary (30 s)'),
  { id: `${startedAt - 3 * 86_400_000}-1`, job_id: null, kind: null, queue: null, task_id: null, attempts: 0, error: 'malformed job entry', dead_at: iso(startedAt - 3 * 86_400_000), enqueued_at: null, payload: null, retryable: false },
]
/** Runs started by retrying dead letters, simulated here (kinds the cleanup mock does not own). */
const retries: { run: JobRunSummary; durationMs: number }[] = []

// ---------------------------------------------------------------- runs

function logFor(run: JobRunSummary): string[] {
  const t0 = Date.parse(run.started_at)
  const at = (ms: number) => new Date(t0 + ms).toISOString().replace('Z', '+00:00')
  const next = rng(hash(run.id))
  const repos = world.repos.filter((r) => r.type === 'proxy').map((r) => r.name)
  const lines: string[] = []
  const shown = run.status === 'running' ? Math.max(1, Math.floor((run.progress.total ? run.progress.done / run.progress.total : 0.4) * 6)) : 6
  if (run.kind === 'blob.gc') {
    lines.push(`${at(200)} mark: scanning ${(148_220).toLocaleString('en')} blobs against assets and manifests`, `${at(4100)} mark: ${Math.round(between(40, 1300, next))} blobs newly unreferenced`, `${at(6400)} sweep: blobs unreferenced for over 24 h`, `${at(8800)} removed ${Math.round(between(0, 4, next))} orphan uploads older than 24 h`)
    if (run.status === 'succeeded') lines.push(`${at(9100)} freed ${String(run.result.freed_blobs ?? 0)} blobs, marked ${String(run.result.marked ?? 0)}, 0 referenced blobs missing`)
  } else if (run.kind === 'cleanup.run') {
    repos.slice(0, 3).forEach((repo, i) => lines.push(`${at(600 + i * 2400 + between(0, 2000, next))} ${repo}: ${Math.round(between(0, 90, next))} candidates (${between(1, 900, next).toFixed(1)} MiB)`))
    if (run.status === 'succeeded') lines.push(`${at(9000)} removed ${String(run.result.removed ?? 0)} items`)
  }
  if (run.error) lines.push(`${at(9500)} ERROR ${run.error}`)
  return lines.slice(0, run.status === 'running' ? shown : undefined)
}

function fromCleanup(policyId: string, r: CleanupRunSummary): JobRunSummary {
  const done = r.finished_at ? Date.parse(r.finished_at) - Date.parse(r.started_at) : null
  return { id: r.id, job_id: `job-${r.id}`, kind: 'cleanup.run', queue: 'maintenance', task_id: `cleanup:${policyId}`, trigger: r.trigger, attempt: 1, status: r.status, started_at: r.started_at, finished_at: r.finished_at, duration_ms: done, progress: r.progress, worker_id: WORKERS[hash(r.id) % 2]!.id, error: r.error, result: r.status === 'succeeded' ? { policy_id: policyId, removed: r.removed, bytes: r.bytes } : {} }
}

function fromGc(r: GcRun): JobRunSummary {
  const done = r.finished_at ? Date.parse(r.finished_at) - Date.parse(r.started_at) : null
  return { id: r.id, job_id: `job-${r.id}`, kind: 'blob.gc', queue: 'maintenance', task_id: 'blob.gc', trigger: r.trigger, attempt: 1, status: r.status, started_at: r.started_at, finished_at: r.finished_at, duration_ms: done, progress: r.progress, worker_id: WORKERS[0]!.id, error: r.error, result: r.status === 'succeeded' ? { freed_blobs: r.freed_blobs, freed_bytes: r.freed_bytes, marked: r.marked, uploads_removed: r.uploads_removed } : {} }
}

/** The 10 s rollup's recent runs, derived from the clock (routine: hidden unless asked for). */
function rollupRuns(now: number): JobRunSummary[] {
  if (!rollup.enabled) return []
  const last = Math.floor(now / 10_000) * 10_000
  return Array.from({ length: ROLLUP_KEEP }, (_, i) => {
    const t0 = last - i * 10_000 + 40
    const ms = 18 + (hash(String(t0)) % 40)
    return { id: `rr_${t0}`, job_id: `job-rr-${t0}`, kind: 'stats.rollup', queue: 'stats', task_id: 'stats.rollup', trigger: 'schedule' as const, attempt: 1, status: 'succeeded' as const, started_at: iso(t0), finished_at: iso(t0 + ms), duration_ms: ms, progress: { done: 1, total: 1, message: null }, worker_id: WORKERS[0]!.id, error: null, result: { minutes: i % 6 === 0 ? 1 : 0 } }
  })
}

function advanceRetries(now: number): void {
  for (const sim of retries) {
    if (sim.run.status !== 'running') continue
    const frac = Math.min(1, (now - Date.parse(sim.run.started_at)) / sim.durationMs)
    const total = sim.run.progress.total ?? 100
    sim.run.progress = { done: Math.floor(frac * total), total, message: frac < 1 ? 'Working' : 'Done' }
    if (frac >= 1) {
      sim.run.status = 'succeeded'
      sim.run.finished_at = iso(Date.parse(sim.run.started_at) + sim.durationMs)
      sim.run.duration_ms = sim.durationMs
      sim.run.result = sim.run.kind === 'blob.gc' ? { freed_blobs: 214, freed_bytes: Math.round(1.7 * GiB), marked: 88, uploads_removed: 0 } : { removed: 31, bytes: Math.round(412 * MiB) }
    }
  }
}

export function allRuns(now: number = Date.now(), includeRoutine = true): JobRunSummary[] {
  advanceRetries(now)
  const runs = [...cleanupTasks(now).flatMap((p) => p.runs.map((r) => fromCleanup(p.id, r))), ...gcTask(now).runs.map(fromGc), ...retries.map((s) => ({ ...s.run }))]
  if (includeRoutine) runs.push(...rollupRuns(now))
  return runs.sort((a, b) => (a.started_at < b.started_at ? 1 : a.started_at > b.started_at ? -1 : a.id < b.id ? 1 : -1))
}

// ---------------------------------------------------------------- tasks, workers, queues

export function scheduledTasks(now: number = Date.now()): ScheduledTask[] {
  const runs = allRuns(now)
  const lastOf = (taskId: string) => runs.find((r) => r.task_id === taskId) ?? null
  const gc = gcTask(now).settings
  const nextOf = (schedule: string, enabled: boolean) => (enabled ? nextFireOf(schedule, now) : null)
  const tasks: ScheduledTask[] = [
    { id: 'blob.gc', kind: 'blob.gc', queue: 'maintenance', schedule: gc.schedule, enabled: gc.enabled, routine: false, params: { grace_s: gc.grace_hours * 3600, upload_age_s: 86400 }, next_run_at: nextOf(gc.schedule, gc.enabled), last_fired_at: lastOf('blob.gc')?.started_at ?? null, last_run: lastOf('blob.gc'), owner: null },
    ...cleanupTasks(now).map((p) => ({ id: `cleanup:${p.id}`, kind: 'cleanup.run', queue: 'maintenance', schedule: p.schedule, enabled: p.enabled, routine: false, params: { policy_id: p.id }, next_run_at: nextOf(p.schedule, p.enabled), last_fired_at: null, last_run: lastOf(`cleanup:${p.id}`), owner: { kind: 'cleanup_policy' as const, id: p.id, name: p.name } })),
    { id: 'stats.rollup', kind: 'stats.rollup', queue: 'stats', schedule: rollup.schedule, enabled: rollup.enabled, routine: true, params: {}, next_run_at: nextOf(rollup.schedule, rollup.enabled), last_fired_at: lastOf('stats.rollup')?.started_at ?? null, last_run: lastOf('stats.rollup'), owner: null },
  ]
  return tasks.sort((a, b) => a.id.localeCompare(b.id))
}

function nextFireOf(schedule: string, now: number): string {
  const every = /^@every (\d+)([smh])$/.exec(schedule)
  if (every) {
    const step = Number(every[1]) * { s: 1000, m: MINUTE, h: 60 * MINUTE }[every[2] as 's' | 'm' | 'h']
    return iso((Math.floor(now / step) + 1) * step)
  }
  const [mi, hr] = schedule.replace('@daily', '0 0 * * *').replace('@hourly', '0 * * * *').split(' ')
  const at = new Date(now)
  at.setUTCSeconds(0, 0)
  for (let i = 0; i < 60 * 24 * 8; i++) {
    at.setUTCMinutes(at.getUTCMinutes() + 1)
    const ok = (field: string | undefined, value: number) => !field || field === '*' || (field.startsWith('*/') ? value % Number(field.slice(2)) === 0 : field.split(',').includes(String(value)))
    if (ok(mi, at.getUTCMinutes()) && ok(hr, at.getUTCHours())) return at.toISOString()
  }
  return iso(now + 86_400_000)
}

export function workerOverview(now: number = Date.now()): { workers: Worker[]; queues: QueueFigures[]; dead_letters: number } {
  const running = allRuns(now, false).filter((r) => r.status === 'running')
  const workers = WORKERS.map((w, i) => {
    const age = ((now / 1000 + i * 3.7) % 10) + 0.4
    return { ...w, heartbeat_at: iso(now - age * 1000), heartbeat_age_s: Math.round(age * 10) / 10, running_jobs: running.filter((r) => r.worker_id === w.id).map((r) => ({ job_id: r.job_id, run_id: r.id, kind: r.kind, queue: r.queue, started_at: r.started_at })) }
  })
  const queues = (['index', 'maintenance', 'stats', 'prefetch', 'import'] as const).map((name) => ({
    name,
    waiting: name === 'prefetch' ? 2 : 0,
    running: running.filter((r) => r.queue === name).length,
    delayed: name === 'index' ? 1 : 0,
    oldest_waiting_age_s: name === 'prefetch' ? 3.2 : 0,
  }))
  return { workers, queues, dead_letters: deadLetters.length }
}

// ---------------------------------------------------------------- live

function emit(event: TaskEvent): void {
  for (const l of listeners) l(event)
}

/** One second of the mock job system: fire a schedule now and then, then report what changed. */
export function taskTick(now: number = Date.now()): void {
  if (now >= nextFire) {
    nextFire = now + between(40_000, 75_000)
    const scheduled = cleanupTasks(now).filter((p) => p.enabled && p.attached.length)
    const pick = scheduled[Math.floor(Math.random() * scheduled.length)]
    if (pick) startTaskRun(pick.id, 'schedule')
  }
  const ts = iso(now)
  for (const run of allRuns(now, false)) {
    const before = seen.get(run.id)
    const lines = logFor(run).length
    seen.set(run.id, { status: run.status, done: run.progress.done, lines })
    if (!before) {
      if (run.status === 'running') emit({ type: 'job.update', ts, event: 'started', run_id: run.id, job_id: run.job_id, kind: run.kind, queue: run.queue, attempt: run.attempt, status: 'running', worker_id: run.worker_id })
      continue
    }
    if (run.status === 'running' && run.progress.done !== before.done) emit({ type: 'job.progress', ts, run_id: run.id, job_id: run.job_id, kind: run.kind, done: run.progress.done, total: run.progress.total, message: run.progress.message })
    if (lines > before.lines) emit({ type: 'job.log', ts, run_id: run.id, job_id: run.job_id, kind: run.kind, lines: logFor(run).slice(before.lines) })
    if (run.status !== before.status) emit({ type: 'job.update', ts, event: run.status === 'succeeded' ? 'finished' : 'failed', run_id: run.id, job_id: run.job_id, kind: run.kind, queue: run.queue, attempt: run.attempt, status: run.status, worker_id: run.worker_id, error: run.error })
  }
}

/** The `tasks` topic in mock mode (mocks/liveSource.ts). */
export function subscribeTasks(listener: Listener): () => void {
  listeners.add(listener)
  if (!seen.size) for (const run of allRuns(Date.now(), false)) seen.set(run.id, { status: run.status, done: run.progress.done, lines: logFor(run).length })
  timer ??= setInterval(() => taskTick(), 1000)
  return () => {
    listeners.delete(listener)
    if (!listeners.size && timer) {
      clearInterval(timer)
      timer = null
    }
  }
}

// ---------------------------------------------------------------- handlers

function runDetail(run: JobRunSummary): JobRun {
  return { ...run, log: logFor(run) }
}

function validate(body: ScheduledTaskUpdate): string | null {
  if (body.schedule === undefined || body.schedule === null) return null
  const text = body.schedule.trim().replace(/\s+/g, ' ')
  const every = /^@every (\d+)([smh])$/i.exec(text)
  if (every) return Number(every[1]) * { s: 1, m: 60, h: 3600 }[every[2]!.toLowerCase() as 's' | 'm' | 'h'] < 10 ? 'not a valid schedule: an @every interval must be at least 10s' : null
  if (text.startsWith('@')) return ['@hourly', '@daily', '@midnight', '@weekly', '@monthly', '@yearly', '@annually'].includes(text.toLowerCase()) ? null : `not a valid schedule: unknown schedule '${text}'`
  const fields = text.split(' ')
  if (fields.length !== 5) return 'not a valid schedule: a cron expression has 5 fields: minute hour day-of-month month day-of-week'
  return fields.every((f) => /^(\*|[0-9a-z]+(-[0-9a-z]+)?)(\/\d+)?(,(\*|[0-9a-z]+(-[0-9a-z]+)?)(\/\d+)?)*$/i.test(f)) ? null : `not a valid schedule: cron value out of range in '${text}'`
}

export function tasksHandlers(h: MockHelpers) {
  return [
    http.get(`${API}/tasks/scheduled`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      return HttpResponse.json({ items: scheduledTasks() })
    }),
    http.patch(`${API}/tasks/scheduled/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const task = scheduledTasks().find((x) => x.id === params.id)
      if (!task) return h.notFound()
      if (task.owner) return h.problem(409, 'Conflict', 'this schedule belongs to a cleanup policy: change it on the Cleanup page', [{ field: 'schedule', message: 'Owned by a cleanup policy', code: 'conflict' }])
      const body = (await request.json()) as ScheduledTaskUpdate
      const error = validate(body)
      if (error) return h.problem(422, 'Unprocessable Content', 'The request has invalid fields.', [{ field: 'schedule', message: error, code: 'value_error' }])
      const schedule = body.schedule ? body.schedule.trim().replace(/\s+/g, ' ').replace(/^@\S+/, (m) => m.toLowerCase()) : undefined
      const target = task.id === 'blob.gc' ? gcTask().settings : rollup
      const changes = []
      if (schedule && schedule !== target.schedule) changes.push({ field: 'schedule', before: target.schedule, after: schedule, redacted: false })
      if (typeof body.enabled === 'boolean' && body.enabled !== target.enabled) changes.push({ field: 'enabled', before: target.enabled, after: body.enabled, redacted: false })
      if (schedule) target.schedule = schedule
      if (typeof body.enabled === 'boolean') target.enabled = body.enabled
      if (changes.length) recordAudit({ action: 'task.update', outcome: 'success', actor: ADMIN, target: { kind: 'task', id: task.id, name: task.id }, changes })
      return HttpResponse.json(scheduledTasks().find((x) => x.id === task.id))
    }),
    http.post(`${API}/tasks/scheduled/:id/run`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const id = String(params.id)
      let accepted: JobAccepted | null = null
      if (id === 'blob.gc') accepted = startTaskRun('gc')
      else if (id.startsWith('cleanup:')) accepted = startTaskRun(id.slice('cleanup:'.length))
      else if (id === 'stats.rollup') accepted = { job_id: `job-rr-${Date.now()}`, status: 'enqueued' }
      if (!accepted) return h.notFound()
      recordAudit({ action: 'task.run', outcome: 'success', actor: ADMIN, target: { kind: 'task', id, name: id }, details: { job_id: accepted.job_id, status: accepted.status } })
      return HttpResponse.json(accepted, { status: 202 })
    }),
    http.get(`${API}/tasks/runs`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const url = new URL(request.url)
      const statuses = url.searchParams.getAll('status')
      const kind = url.searchParams.get('kind')
      const task = url.searchParams.get('task')
      const job = url.searchParams.get('job')
      const routine = url.searchParams.get('include_routine') === 'true' || Boolean(task) || Boolean(job)
      const limit = Math.min(200, Math.max(1, Number(url.searchParams.get('limit') ?? 50)))
      const offset = Number(atob(url.searchParams.get('cursor') ?? '') || 0)
      const items = allRuns(Date.now(), routine).filter((r) => (!statuses.length || statuses.includes(r.status)) && (!kind || r.kind === kind) && (!task || r.task_id === task) && (!job || r.job_id === job))
      const page = items.slice(offset, offset + limit)
      return HttpResponse.json({ items: page, next_cursor: offset + limit < items.length ? btoa(String(offset + limit)) : null })
    }),
    http.get(`${API}/tasks/runs/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const run = allRuns().find((r) => r.id === params.id)
      return run ? HttpResponse.json(runDetail(run)) : h.notFound()
    }),
    http.get(`${API}/tasks/workers`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      return HttpResponse.json(workerOverview())
    }),
    http.get(`${API}/tasks/dead-letters`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const url = new URL(request.url)
      const limit = Math.min(200, Math.max(1, Number(url.searchParams.get('limit') ?? 50)))
      const cursor = url.searchParams.get('cursor')
      const sorted = [...deadLetters].sort((a, b) => (a.dead_at < b.dead_at ? 1 : -1))
      const start = cursor ? sorted.findIndex((d) => d.id === cursor) + 1 : 0
      const items = sorted.slice(start, start + limit)
      return HttpResponse.json({ items, total: deadLetters.length, next_cursor: start + limit < sorted.length ? items[items.length - 1]!.id : null })
    }),
    http.post(`${API}/tasks/dead-letters/:id/retry`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const letter = deadLetters.find((d) => d.id === params.id)
      if (!letter) return h.notFound()
      if (!letter.retryable || !letter.kind) return h.problem(409, 'Conflict', 'this dead letter cannot be retried (malformed, or a job kind this version does not know)')
      deadLetters.splice(deadLetters.indexOf(letter), 1)
      const now = Date.now()
      const run: JobRunSummary = { id: `rt_${now}`, job_id: `01JCRETRY${now}`, kind: letter.kind, queue: letter.queue ?? 'maintenance', task_id: letter.task_id, trigger: 'retry', attempt: 1, status: 'running', started_at: iso(now), finished_at: null, duration_ms: null, progress: { done: 0, total: 100, message: 'Queued' }, worker_id: WORKERS[1]!.id, error: null, result: {} }
      retries.unshift({ run, durationMs: 12_000 })
      recordAudit({ action: 'task.retry', outcome: 'success', actor: ADMIN, target: { kind: 'job', id: letter.job_id, name: letter.kind }, details: { dead_letter: letter.id, job_id: run.job_id, status: 'enqueued' } })
      return HttpResponse.json({ job_id: run.job_id, status: 'enqueued' } satisfies JobAccepted, { status: 202 })
    }),
    http.delete(`${API}/tasks/dead-letters/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const letter = deadLetters.find((d) => d.id === params.id)
      if (letter) {
        deadLetters.splice(deadLetters.indexOf(letter), 1)
        recordAudit({ action: 'task.discard', outcome: 'success', actor: ADMIN, target: { kind: 'job', id: letter.job_id, name: letter.kind }, details: { dead_letter: letter.id } })
      }
      return new HttpResponse(null, { status: 204 })
    }),
  ]
}
