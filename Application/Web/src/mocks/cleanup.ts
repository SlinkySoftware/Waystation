// SPDX-License-Identifier: GPL-3.0-or-later
// Mock cleanup policies and blob GC (contract tag `cleanup`): seeded policies mirroring a home lab's Nexus cleanup
// set, run history, attach per repository, a preview evaluated against the mock inventory, and runs / GC that
// progress over a few seconds (advanced on read, no timers) and then really remove what the preview showed.
import { http, HttpResponse } from 'msw'

import type { CleanupCriteria, CleanupFormat, CleanupPolicy, CleanupPolicyWrite, CleanupPreviewRequest, CleanupRunSummary, FieldError, GcRun, GcStatus, JobAccepted, Repository, RepositoryCleanupUpdate } from '@/api/types'

import { evaluate, validateCriteria } from './fixtures/cleanup'
import { between, GiB, hash, MiB, rng } from './rng'
import type { MockHelpers } from './tokens'
import { world } from './world'

const API = '/api/v1'
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$/
const FORMATS: readonly string[] = ['*', 'docker', 'npm', 'apt', 'raw', 'yum', 'pypi', 'maven', 'go', 'helm', 'terraform', 'galaxy', 'swift', 'nuget', 'cargo', 'rubygems', 'composer', 'conda', 'apk', 'huggingface']
const HOUR = 3_600_000
const DAY = 24 * HOUR
const MACROS: Record<string, string> = { '@hourly': '0 * * * *', '@daily': '0 0 * * *', '@midnight': '0 0 * * *', '@weekly': '0 0 * * 0', '@monthly': '0 0 1 * *' }

type Policy = Omit<CleanupPolicy, 'repositories' | 'next_run_at' | 'last_run'>

interface SimRun {
  run: CleanupRunSummary
  durationMs: number
  /** Decided on the first advance: what the run removes (equals a preview at that moment). */
  plan?: { removed: number; bytes: number; keys: string[]; repos: string[]; error: string | null }
}

interface SimGc {
  run: GcRun
  durationMs: number
}

let seq = 0
const nextId = (prefix: string) => `${prefix}_01JC${String(Date.now()).slice(-8)}${String(++seq).padStart(8, '0')}`
const iso = (ms: number) => new Date(ms).toISOString()

const policies: Policy[] = []
const runs = new Map<string, SimRun[]>()
/** Repository → attached policy ids, in run order. */
const attachments = new Map<string, string[]>()
/** Inventory keys removed by finished runs. */
const removed = new Set<string>()
/** Blobs waiting out the grace period, by when they became unreferenced. */
const pendingBatches: { blobs: number; bytes: number; markedAt: number }[] = []
const gcRuns: SimGc[] = []
const gc = { enabled: true, schedule: '0 5 * * *', grace_hours: 24 }

/** The next time a 5-field cron (minute hour day-of-month * day-of-week, UTC) or macro fires. Enough for the presets. */
export function nextRun(schedule: string, now: number = Date.now()): string {
  const [mi, hr, dom, , dow] = (MACROS[schedule] ?? schedule).split(/\s+/)
  const at = new Date(now)
  at.setUTCSeconds(0, 0)
  for (let step = 0; step < 60 * 24 * 40; step++) {
    at.setUTCMinutes(at.getUTCMinutes() + 1)
    const ok = (field: string | undefined, value: number) => !field || field === '*' || field.split(',').includes(String(value))
    if (ok(mi, at.getUTCMinutes()) && ok(hr, at.getUTCHours()) && ok(dom, at.getUTCDate()) && ok(dow, at.getUTCDay())) return at.toISOString()
  }
  return iso(now + DAY)
}

function criteria(partial: Partial<CleanupCriteria>): CleanupCriteria {
  return { last_downloaded_older_than_days: null, published_older_than_days: null, keep_latest_n_versions: null, name_include: null, name_exclude: null, version_include: null, version_exclude: null, prerelease_only: false, untagged_older_than_days: null, ...partial }
}

function seedPolicy(name: string, format: CleanupFormat, description: string, crit: Partial<CleanupCriteria>, schedule: string | null, repos: string[], history: { every: number; ok?: number; mib: [number, number]; count: [number, number]; failAt?: number; runningNow?: boolean }, enabled = true): void {
  const id = `cp_${String(policies.length + 1).padStart(4, '0')}${name.replace(/[^a-z0-9]/g, '').slice(0, 12)}`
  const now = Date.now()
  policies.push({ id, name, description, format, criteria: criteria(crit), schedule, enabled, created_at: iso(now - 40 * DAY), updated_at: iso(now - 9 * DAY) })
  for (const repo of repos) attachments.set(repo, [...(attachments.get(repo) ?? []), id])
  const next = rng(hash(`runs:${name}`))
  const list: SimRun[] = []
  if (history.runningNow) {
    list.push({ run: { id: nextId('cr'), status: 'running', trigger: 'schedule', started_at: iso(now - 14_000), finished_at: null, progress: { done: 0, total: null, message: 'Evaluating' }, removed: 0, bytes: 0, error: null }, durationMs: 26_000 })
  }
  // Policies never run yet (count range 0..0) have no history.
  const count = history.count[1] === 0 ? 0 : history.every ? Math.min(12, Math.floor(36 / history.every)) : 2
  for (let i = 0; i < count; i++) {
    const started = now - (i + 1) * (history.every || 11) * DAY - next() * 2 * HOUR
    const seconds = between(6, 140, next)
    const failed = history.failAt === i
    const removedCount = failed ? 0 : Math.round(between(...history.count, next))
    list.push({
      run: {
        id: nextId('cr'),
        status: failed ? 'failed' : 'succeeded',
        trigger: history.every ? 'schedule' : 'manual',
        started_at: iso(started),
        finished_at: iso(started + seconds * 1000),
        progress: { done: removedCount, total: removedCount, message: failed ? null : 'Done' },
        removed: removedCount,
        bytes: failed ? 0 : Math.round(between(...history.mib, next) * MiB),
        error: failed ? 'Upstream index for library/postgres could not be read (timeout after 30 s); nothing was removed.' : null,
      },
      durationMs: seconds * 1000,
    })
  }
  runs.set(id, list)
}

seedPolicy('docker-untagged-14d', 'docker', 'Manifests no tag points to any more (superseded :latest pulls), after two weeks.', { untagged_older_than_days: 14 }, '0 2 * * *', ['dockerhub-proxy', 'ghcr-proxy'], { every: 1, mib: [40, 900], count: [3, 60] })
seedPolicy('docker-keep-latest-5', 'docker', 'Keep the five newest tags of each image; older tags go once unused for 60 days. Databases are protected.', { keep_latest_n_versions: 5, last_downloaded_older_than_days: 60, name_exclude: '^library/(postgres|mongo)$' }, '0 3 * * 0', ['dockerhub-proxy'], { every: 7, mib: [800, 6200], count: [20, 180], failAt: 2 })
seedPolicy('npm-unused-90d', 'npm', 'Tarballs nobody has installed for 90 days.', { last_downloaded_older_than_days: 90 }, '0 3 * * *', ['npm'], { every: 1, mib: [12, 380], count: [8, 140] })
seedPolicy('npm-prereleases-30d', 'npm', 'Betas, release candidates and canaries are short-lived.', { prerelease_only: true, published_older_than_days: 30 }, '0 3 * * 0', ['npm'], { every: 7, mib: [2, 60], count: [2, 30] })
seedPolicy('apt-old-debs-180d', 'apt', 'Superseded .deb versions unused for six months; the two newest of each package stay.', { last_downloaded_older_than_days: 180, keep_latest_n_versions: 2 }, '0 4 * * 0', ['debian', 'ubuntu', 'raspberrypi'], { every: 7, mib: [300, 2600], count: [40, 400], runningNow: true })
seedPolicy('raw-keep-latest-3', 'raw', 'Release downloads: keep the three newest versions of each project.', { keep_latest_n_versions: 3 }, null, ['raw-releases'], { every: 0, mib: [20, 140], count: [2, 9] })
seedPolicy('all-unused-365d', '*', 'Safety net for every format: anything untouched for a year. Not attached yet.', { last_downloaded_older_than_days: 365 }, '0 4 1 * *', [], { every: 0, mib: [0, 0], count: [0, 0] }, false)

// GC: two batches waiting (one already past the grace period), a daily history with one interrupted run.
{
  const now = Date.now()
  pendingBatches.push({ blobs: 412, bytes: Math.round(3.1 * GiB), markedAt: now - 30 * HOUR }, { blobs: 872, bytes: Math.round(6.6 * GiB), markedAt: now - 6 * HOUR })
  const next = rng(hash('gc-runs'))
  const today5 = new Date(now)
  today5.setUTCHours(5, 0, 0, 0)
  let start = today5.getTime() > now ? today5.getTime() - DAY : today5.getTime()
  for (let i = 0; i < 14; i++) {
    const seconds = between(20, 260, next)
    const interrupted = i === 4
    gcRuns.push({
      run: {
        id: nextId('gc'),
        status: interrupted ? 'interrupted' : 'succeeded',
        trigger: i === 6 ? 'manual' : 'schedule',
        started_at: iso(start + next() * 30_000),
        finished_at: iso(start + seconds * 1000),
        progress: { done: interrupted ? 61_204 : 148_220, total: 148_220, message: interrupted ? 'Worker restarted during the sweep' : 'Done' },
        freed_blobs: interrupted ? 0 : Math.round(between(30, 1600, next)),
        freed_bytes: interrupted ? 0 : Math.round(between(0.2, 14, next) * GiB),
        marked: Math.round(between(20, 1400, next)),
        uploads_removed: Math.round(between(0, 4, next)),
        error: interrupted ? 'Interrupted: the worker restarted during the sweep. Nothing was lost; the next run picks up the remaining blobs.' : null,
      },
      durationMs: seconds * 1000,
    })
    start -= DAY
  }
}

function attachedTo(id: string): string[] {
  return [...attachments].filter(([, ids]) => ids.includes(id)).map(([repo]) => repo).sort()
}

function finishRun(policy: Policy, sim: SimRun, now: number): void {
  const plan = sim.plan!
  sim.run = { ...sim.run, status: plan.error ? 'failed' : 'succeeded', finished_at: iso(Date.parse(sim.run.started_at) + sim.durationMs), progress: { done: plan.removed, total: plan.removed, message: plan.error ? null : 'Done' }, removed: plan.error ? 0 : plan.removed, bytes: plan.error ? 0 : plan.bytes, error: plan.error }
  if (plan.error) return
  plan.keys.forEach((k) => removed.add(k))
  if (plan.bytes > 0) pendingBatches.push({ blobs: plan.removed, bytes: plan.bytes, markedAt: now })
  for (const name of plan.repos) {
    const repo = world.repos.find((r) => r.name === name)
    if (repo) repo.stats = { ...repo.stats, size_bytes: Math.max(0, repo.stats.size_bytes - Math.round(plan.bytes / Math.max(1, plan.repos.length))) }
  }
  world.addActivity({ kind: 'cleanup', summary: `Cleanup policy "${policy.name}" removed ${plan.removed} items`, outcome: 'success', repository: plan.repos.length === 1 ? plan.repos[0]! : null, format: policy.format === '*' ? null : policy.format, artifact: null, principal: sim.run.trigger === 'manual' ? 'admin' : 'scheduler', cache: null, bytes: plan.bytes, duration_ms: sim.durationMs })
}

/** Move running runs forward to `now`; finished runs apply their removals exactly once. */
function advance(now: number = Date.now()): void {
  for (const policy of policies) {
    for (const sim of runs.get(policy.id) ?? []) {
      if (sim.run.status !== 'running') continue
      if (!sim.plan) {
        const repos = scopeOf(policy.format, attachedTo(policy.id))
        const result = evaluate(policy.criteria, repos, removed, now)
        sim.plan = { removed: result.candidates.length, bytes: result.candidates.reduce((a, c) => a + c.size, 0), keys: result.keys, repos: repos.map((r) => r.name), error: null }
      }
      const frac = Math.min(1, (now - Date.parse(sim.run.started_at)) / sim.durationMs)
      if (frac >= 1) finishRun(policy, sim, now)
      else {
        const repo = sim.plan.repos[Math.min(sim.plan.repos.length - 1, Math.floor(frac * sim.plan.repos.length))]
        sim.run = { ...sim.run, progress: { done: Math.floor(frac * sim.plan.removed), total: sim.plan.removed, message: repo ? `Removing from ${repo}` : 'Nothing attached' }, removed: Math.floor(frac * sim.plan.removed), bytes: Math.floor(frac * sim.plan.bytes) }
      }
    }
  }
  for (const sim of gcRuns) {
    if (sim.run.status !== 'running') continue
    const frac = Math.min(1, (now - Date.parse(sim.run.started_at)) / sim.durationMs)
    const total = sim.run.progress.total ?? 1
    if (frac < 1) {
      sim.run = { ...sim.run, progress: { done: Math.floor(frac * total), total, message: frac < 0.5 ? 'Marking unreferenced blobs' : 'Sweeping blobs past the grace period' } }
      continue
    }
    const due = pendingBatches.filter((b) => now - b.markedAt >= gc.grace_hours * HOUR)
    due.forEach((b) => pendingBatches.splice(pendingBatches.indexOf(b), 1))
    const freedBytes = due.reduce((a, b) => a + b.bytes, 0)
    world.storageUsed = Math.max(0, world.storageUsed - freedBytes)
    sim.run = { ...sim.run, status: 'succeeded', finished_at: iso(Date.parse(sim.run.started_at) + sim.durationMs), progress: { done: total, total, message: 'Done' }, freed_blobs: due.reduce((a, b) => a + b.blobs, 0), freed_bytes: freedBytes, marked: 0, uploads_removed: 1 }
  }
}

function scopeOf(format: CleanupFormat, names: string[] | null): Repository[] {
  const pool = names ? world.repos.filter((r) => names.includes(r.name)) : world.repos
  return pool.filter((r) => format === '*' || r.format === format).sort((a, b) => a.name.localeCompare(b.name))
}

function lastRun(id: string): CleanupRunSummary | null {
  return runs.get(id)?.[0]?.run ?? null
}

function toPolicy(p: Policy): CleanupPolicy {
  return { ...p, repositories: attachedTo(p.id), next_run_at: p.enabled && p.schedule ? nextRun(p.schedule) : null, last_run: lastRun(p.id) }
}

function scheduleError(schedule: string | null | undefined): string | null {
  if (!schedule) return null
  if (schedule.startsWith('@')) return MACROS[schedule] ? null : 'Unknown macro. Use @hourly, @daily, @weekly or @monthly.'
  const fields = schedule.trim().split(/\s+/)
  if (fields.length !== 5) return 'Use five fields: minute hour day-of-month month day-of-week (UTC).'
  return fields.every((f) => /^(\*|\d+(-\d+)?)(\/\d+)?(,(\*|\d+(-\d+)?)(\/\d+)?)*$/.test(f)) ? null : 'One of the fields is not valid cron syntax.'
}

export function validatePolicy(body: CleanupPolicyWrite, selfId?: string): FieldError[] {
  const errors: FieldError[] = []
  if (!NAME_RE.test(body.name ?? '')) errors.push({ field: 'name', message: 'Use 2-63 letters, digits, dots, dashes or underscores, starting with a letter or digit.', code: 'pattern' })
  else if (policies.some((p) => p.id !== selfId && p.name.toLowerCase() === body.name.toLowerCase())) errors.push({ field: 'name', message: 'A policy with this name already exists.', code: 'taken' })
  if ((body.description ?? '').length > 500) errors.push({ field: 'description', message: 'Keep the description under 500 characters.', code: 'length' })
  if (!FORMATS.includes(body.format)) errors.push({ field: 'format', message: 'Unknown format.', code: 'enum' })
  errors.push(...validateCriteria(body.criteria ?? criteria({}), body.format))
  const cron = scheduleError(body.schedule)
  if (cron) errors.push({ field: 'schedule', message: cron, code: 'cron' })
  return errors
}

// ---------------------------------------------------------------- bridge for the Tasks mock (mocks/tasks.ts)

/** Scheduled policies (each is the task `cleanup:<id>`) with their runs, newest first; runs advanced to now. */
export function cleanupTasks(now: number = Date.now()): { id: string; name: string; schedule: string; enabled: boolean; attached: string[]; runs: CleanupRunSummary[] }[] {
  advance(now)
  return policies
    .filter((p) => p.schedule)
    .map((p) => ({ id: p.id, name: p.name, schedule: p.schedule!, enabled: p.enabled, attached: attachedTo(p.id), runs: (runs.get(p.id) ?? []).map((s) => s.run) }))
}

/** Blob GC settings (shared with the GC card, so a schedule edited on the Tasks page shows there) and runs. */
export function gcTask(now: number = Date.now()): { settings: typeof gc; runs: GcRun[] } {
  advance(now)
  return { settings: gc, runs: gcRuns.map((s) => s.run) }
}

/** Run a policy or GC now (Tasks page "Run now"); merges into a running one like the real job system. */
export function startTaskRun(task: 'gc' | string, trigger: 'schedule' | 'manual' = 'manual'): JobAccepted | null {
  advance()
  if (task === 'gc') {
    const current = gcRuns.find((s) => s.run.status === 'running')
    if (current) return { job_id: current.run.id, status: 'merged' }
    const run: GcRun = { id: nextId('gc'), status: 'running', trigger, started_at: iso(Date.now()), finished_at: null, progress: { done: 0, total: 148_220, message: 'Marking unreferenced blobs' }, freed_blobs: 0, freed_bytes: 0, marked: 0, uploads_removed: 0, error: null }
    gcRuns.unshift({ run, durationMs: 9000 })
    return { job_id: run.id, status: 'enqueued' }
  }
  const p = policies.find((x) => x.id === task)
  if (!p) return null
  const list = runs.get(p.id) ?? []
  const current = list.find((r) => r.run.status === 'running')
  if (current) return { job_id: current.run.id, status: 'merged' }
  const sim: SimRun = { run: { id: nextId('cr'), status: 'running', trigger, started_at: iso(Date.now()), finished_at: null, progress: { done: 0, total: null, message: 'Queued' }, removed: 0, bytes: 0, error: null }, durationMs: 9000 + Math.random() * 4000 }
  runs.set(p.id, [sim, ...list].slice(0, 50))
  return { job_id: sim.run.id, status: 'enqueued' }
}

export function cleanupHandlers(h: MockHelpers) {
  const invalid = (errors: FieldError[]) => h.problem(422, 'Unprocessable Content', 'The request has invalid fields.', errors)
  const find = (id: unknown) => policies.find((p) => p.id === id)

  function save(body: CleanupPolicyWrite, existing?: Policy): Response {
    const errors = validatePolicy(body, existing?.id)
    if (errors.some((e) => e.code === 'taken')) return h.problem(409, 'Conflict', 'A policy with this name already exists.', errors)
    if (errors.length) return invalid(errors)
    const now = iso(Date.now())
    const next: Policy = { id: existing?.id ?? nextId('cp'), name: body.name, description: body.description ?? '', format: body.format, criteria: criteria(body.criteria), schedule: body.schedule ?? null, enabled: body.enabled ?? true, created_at: existing?.created_at ?? now, updated_at: now }
    if (existing) policies.splice(policies.indexOf(existing), 1, next)
    else {
      policies.push(next)
      runs.set(next.id, [])
    }
    // A format change detaches the policy from repositories it no longer fits.
    for (const [repo, ids] of attachments) {
      const format = world.repos.find((r) => r.name === repo)?.format
      if (ids.includes(next.id) && format && next.format !== '*' && next.format !== format) attachments.set(repo, ids.filter((i) => i !== next.id))
    }
    return HttpResponse.json(toPolicy(next), { status: existing ? 200 : 201 })
  }

  return [
    http.get(`${API}/repositories/:name/cleanup`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      advance()
      const name = String(params.name)
      if (!world.repos.some((r) => r.name === name)) return h.notFound()
      return HttpResponse.json({ repository: name, policies: (attachments.get(name) ?? []).map(find).filter((p): p is Policy => Boolean(p)).map(toPolicy) })
    }),
    http.put(`${API}/repositories/:name/cleanup`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const repo = world.repos.find((r) => r.name === String(params.name))
      if (!repo) return h.notFound()
      const body = (await request.json()) as RepositoryCleanupUpdate
      const ids = [...new Set(body.policy_ids ?? [])]
      const errors: FieldError[] = []
      if (ids.length > 20) errors.push({ field: 'policy_ids', message: 'Attach at most 20 policies.', code: 'max' })
      ids.forEach((id, i) => {
        const p = find(id)
        if (!p) errors.push({ field: `policy_ids.${i}`, message: 'Unknown policy.', code: 'unknown' })
        else if (p.format !== '*' && p.format !== repo.format) errors.push({ field: `policy_ids.${i}`, message: `"${p.name}" is a ${p.format} policy; ${repo.name} is ${repo.format}.`, code: 'format' })
      })
      if (errors.length) return invalid(errors)
      attachments.set(repo.name, ids)
      return HttpResponse.json({ repository: repo.name, policies: ids.map((id) => toPolicy(find(id)!)) })
    }),
    http.get(`${API}/cleanup-policies`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      advance()
      return HttpResponse.json({ items: [...policies].sort((a, b) => a.name.localeCompare(b.name)).map(toPolicy) })
    }),
    http.post(`${API}/cleanup-policies`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      return save((await request.json()) as CleanupPolicyWrite)
    }),
    http.post(`${API}/cleanup-policies/preview`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      advance()
      const body = (await request.json()) as CleanupPreviewRequest
      const errors = validateCriteria(criteria(body.criteria ?? {}), body.format)
      if (errors.length) return invalid(errors)
      let names: string[] | null = null
      if (body.repositories?.length) {
        if (body.repositories.some((n) => !world.repos.some((r) => r.name === n))) return h.notFound()
        names = body.repositories
      } else if (body.policy_id) {
        if (!find(body.policy_id)) return h.notFound()
        names = attachedTo(body.policy_id)
      }
      const result = evaluate(criteria(body.criteria), scopeOf(body.format, names), removed)
      const limit = Math.min(500, Math.max(0, body.limit ?? 50))
      const offset = Number(atob(body.cursor ?? '') || 0)
      const items = limit === 0 ? [] : result.candidates.slice(offset, offset + limit)
      const nextCursor = limit > 0 && offset + limit < result.candidates.length ? btoa(String(offset + limit)) : null
      return HttpResponse.json({ count: result.candidates.length, bytes: result.candidates.reduce((a, c) => a + c.size, 0), truncated: false, repositories: result.totals, items, next_cursor: nextCursor })
    }),
    http.get(`${API}/cleanup-policies/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      advance()
      const p = find(params.id)
      return p ? HttpResponse.json(toPolicy(p)) : h.notFound()
    }),
    http.put(`${API}/cleanup-policies/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const p = find(params.id)
      return p ? save((await request.json()) as CleanupPolicyWrite, p) : h.notFound()
    }),
    http.delete(`${API}/cleanup-policies/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const p = find(params.id)
      if (!p) return h.notFound()
      policies.splice(policies.indexOf(p), 1)
      runs.delete(p.id)
      for (const [repo, ids] of attachments) attachments.set(repo, ids.filter((i) => i !== p.id))
      return new HttpResponse(null, { status: 204 })
    }),
    http.post(`${API}/cleanup-policies/:id/run`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      advance()
      const p = find(params.id)
      if (!p) return h.notFound()
      const list = runs.get(p.id) ?? []
      const current = list.find((r) => r.run.status === 'running')
      if (current) return HttpResponse.json({ job_id: current.run.id, status: 'merged' } satisfies JobAccepted, { status: 202 })
      const sim: SimRun = { run: { id: nextId('cr'), status: 'running', trigger: 'manual', started_at: iso(Date.now()), finished_at: null, progress: { done: 0, total: null, message: 'Queued' }, removed: 0, bytes: 0, error: null }, durationMs: 4500 + Math.random() * 3000 }
      runs.set(p.id, [sim, ...list].slice(0, 50))
      return HttpResponse.json({ job_id: sim.run.id, status: 'enqueued' } satisfies JobAccepted, { status: 202 })
    }),
    http.get(`${API}/cleanup-policies/:id/runs`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      advance()
      const p = find(params.id)
      return p ? HttpResponse.json({ items: (runs.get(p.id) ?? []).map((s) => s.run) }) : h.notFound()
    }),
    http.get(`${API}/maintenance/gc`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      advance()
      const status: GcStatus = {
        ...gc,
        next_run_at: gc.enabled ? nextRun(gc.schedule) : null,
        pending_blobs: pendingBatches.reduce((a, b) => a + b.blobs, 0),
        pending_bytes: pendingBatches.reduce((a, b) => a + b.bytes, 0),
        runs: gcRuns.slice(0, 20).map((s) => s.run),
      }
      return HttpResponse.json(status)
    }),
    http.post(`${API}/maintenance/gc/run`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      advance()
      const current = gcRuns.find((s) => s.run.status === 'running')
      if (current) return HttpResponse.json({ job_id: current.run.id, status: 'merged' } satisfies JobAccepted, { status: 202 })
      const run: GcRun = { id: nextId('gc'), status: 'running', trigger: 'manual', started_at: iso(Date.now()), finished_at: null, progress: { done: 0, total: 148_220, message: 'Marking unreferenced blobs' }, freed_blobs: 0, freed_bytes: 0, marked: 0, uploads_removed: 0, error: null }
      gcRuns.unshift({ run, durationMs: 6000 })
      return HttpResponse.json({ job_id: run.id, status: 'enqueued' } satisfies JobAccepted, { status: 202 })
    }),
  ]
}
