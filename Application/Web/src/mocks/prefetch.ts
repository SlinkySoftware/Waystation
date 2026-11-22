// SPDX-License-Identifier: GPL-3.0-or-later
// Mock prefetch (T-O12): the status of a proxy's prefetch (GET /repositories/{name}/prefetch), "Warm now"
// (POST .../prefetch/run) and the warm runs behind them. Seeded with a scheduled warm run of the Docker Hub proxy and a
// manual one of the npm proxy; a run started here finishes after a few seconds. The tasks mock lists these runs and,
// for repositories with a schedule, a `prefetch:<repository id>` task owned by the repository.
import { http, HttpResponse } from 'msw'

import type { FieldError, JobAccepted, PrefetchConfig, PrefetchRun, PrefetchStatus, Repository, RepositoryWrite } from '@/api/types'

import { DEFAULT_PREFETCH } from './fixtures/repositories'
import { hash, rng } from './rng'
import { nextFireOf } from './schedule'
import type { MockHelpers } from './tokens'
import { world } from './world'

const API = '/api/v1'
const iso = (ms: number) => new Date(ms).toISOString()
const RUN_MS = 9_000

/** Formats whose proxies can warm items and refresh popular metadata (the server's format plugins). */
export const PREFETCH_FORMATS: readonly string[] = ['docker', 'npm', 'raw', 'maven', 'go', 'nuget', 'composer', 'cargo', 'conda']

/** A stable, ULID-looking repository id (the API exposes names; scheduled tasks are keyed by id). */
export function repositoryId(name: string): string {
  return `01JCREPO${hash(name).toString(36).toUpperCase().padStart(8, '0')}`
}

export function prefetchOf(repo: Repository): PrefetchConfig {
  return repo.proxy?.prefetch ?? DEFAULT_PREFETCH
}

interface Sim {
  repo: string
  run: PrefetchRun
}

const runs: Sim[] = []
let seeded = false

function seed(now: number): void {
  if (seeded) return
  seeded = true
  const hub = world.repos.find((r) => r.name === 'dockerhub-proxy')
  if (hub) {
    // Last night's 03:00 UTC run.
    const night = new Date(now)
    night.setUTCHours(3, 0, 0, 0)
    if (night.getTime() > now) night.setUTCDate(night.getUTCDate() - 1)
    const t0 = night.getTime() + 2_000
    runs.push({ repo: hub.name, run: { id: `pw_${t0}`, status: 'succeeded', trigger: 'schedule', started_at: iso(t0), finished_at: iso(t0 + 41_000), items: 4, failed: 0, files: 38, fetched: 7, error: null } })
  }
  const npm = world.repos.find((r) => r.name === 'npm')
  if (npm) {
    const t0 = now - 2 * 86_400_000 - 5 * 3_600_000
    runs.push({ repo: npm.name, run: { id: `pw_${t0}`, status: 'succeeded', trigger: 'manual', started_at: iso(t0), finished_at: iso(t0 + 6_400), items: 4, failed: 1, files: 7, fetched: 3, error: null } })
  }
}

/** Move running warm runs on: they finish RUN_MS after they started. */
function advance(now: number): void {
  for (const sim of runs) {
    const run = sim.run
    if (run.status !== 'running' || now - Date.parse(run.started_at) < RUN_MS) continue
    const repo = world.repos.find((r) => r.name === sim.repo)
    const items = repo ? (prefetchOf(repo).warm ?? []).length : run.items
    const next = rng(hash(run.id))
    const perItem = repo?.format === 'docker' ? 9 : repo?.format === 'maven' ? 3 : 2
    run.status = 'succeeded'
    run.finished_at = iso(Date.parse(run.started_at) + RUN_MS)
    run.items = items
    run.failed = 0
    run.files = items * perItem
    run.fetched = Math.round(run.files * (0.05 + next() * 0.25))
  }
}

/** Every warm run, newest first, with its repository (the tasks mock turns these into job runs). */
export function prefetchRuns(now: number = Date.now()): { repo: string; repoId: string; run: PrefetchRun }[] {
  seed(now)
  advance(now)
  return runs
    .map((s) => ({ repo: s.repo, repoId: repositoryId(s.repo), run: { ...s.run } }))
    .sort((a, b) => (a.run.started_at < b.run.started_at ? 1 : -1))
}

/** Repositories whose warm list runs on a schedule (the tasks mock lists them as owned scheduled tasks). */
export function scheduledWarmRepos(): Repository[] {
  return world.repos.filter((r) => r.type === 'proxy' && PREFETCH_FORMATS.includes(r.format) && prefetchOf(r).schedule && (prefetchOf(r).warm ?? []).length)
}

export function prefetchStatus(repo: Repository, now: number = Date.now()): PrefetchStatus {
  const config = prefetchOf(repo)
  const supported = PREFETCH_FORMATS.includes(repo.format)
  const items = (config.warm ?? []).length
  const schedule = config.schedule ?? null
  return {
    warm_supported: supported,
    refresh_supported: supported,
    warm_items: items,
    refresh_popular: config.refresh_popular,
    schedule,
    next_run_at: schedule && items ? nextFireOf(schedule, now) : null,
    last_run: prefetchRuns(now).find((r) => r.repo === repo.name)?.run ?? null,
  }
}

/** Start a warm run of `repo` (or join the one in progress). */
export function startWarm(repo: Repository, trigger: PrefetchRun['trigger'] = 'manual', now: number = Date.now()): JobAccepted {
  seed(now)
  advance(now)
  const running = runs.find((s) => s.repo === repo.name && s.run.status === 'running')
  if (running) return { job_id: `job-${running.run.id}`, status: 'merged' }
  const id = `pw_${now}`
  runs.push({ repo: repo.name, run: { id, status: 'running', trigger, started_at: iso(now), finished_at: null, items: (prefetchOf(repo).warm ?? []).length, failed: 0, files: 0, fetched: 0, error: null } })
  return { job_id: `job-${id}`, status: 'enqueued' }
}

/** Progress of a warm run for the job view: items done so far. */
export function warmProgress(run: PrefetchRun, now: number = Date.now()): { done: number; total: number; message: string | null } {
  if (run.status !== 'running') return { done: run.items, total: run.items, message: null }
  const frac = Math.min(1, (now - Date.parse(run.started_at)) / RUN_MS)
  return { done: Math.floor(frac * run.items), total: run.items, message: 'Warming' }
}

// ---------------------------------------------------------------- validation (the server's, in brief)

const EVERY = /^@every\s+(\d+)\s*([smh])$/i
const MACROS = ['@hourly', '@daily', '@midnight', '@weekly', '@monthly', '@yearly', '@annually']

function itemError(format: string, item: string): string | null {
  if (item.length > 512) return 'at most 512 characters'
  switch (format) {
    case 'npm':
      return /[\s^~<>=|*]/.test(item.replace(/^@/, '')) ? 'name, name@version or name@dist-tag (no ranges)' : null
    case 'maven': {
      const parts = item.split(':')
      if (parts.length < 3 || parts.length > 5 || parts.some((p) => !p)) return 'groupId:artifactId:version[:extension[:classifier]]'
      return parts[2]!.endsWith('-SNAPSHOT') ? 'snapshots cannot be warmed' : null
    }
    case 'go':
      return /^[^@\s]+@[^@\s]+$/.test(item) ? null : 'module@version or module@latest'
    case 'cargo':
      return /^[A-Za-z][A-Za-z0-9_-]{0,63}(@\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)?$/.test(item) ? null : 'crate@version (an exact SemVer version) or crate'
    case 'conda':
      return /^[a-z0-9]+(-[a-z0-9]+)?\/[a-z0-9_][a-z0-9_.+-]*(==[A-Za-z0-9_.+!*]+(=[A-Za-z0-9_.+]+)?)?$/.test(item) ? null : 'subdir/name, subdir/name==version or subdir/name==version=build'
    case 'nuget':
      return /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}(@\d+(\.\d+){0,3}(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?)?$/.test(item) ? null : 'Package or Package@version'
    case 'composer':
      return /^[a-z0-9]([_.-]?[a-z0-9]+)*\/[a-z0-9](([_.]|-{1,2})?[a-z0-9]+)*(:[^\s:]{1,100})?$/i.test(item) ? null : 'vendor/name or vendor/name:version'
    case 'raw':
      return item.split('/').includes('..') || item.startsWith('/') ? 'a relative path in the repository, without ..' : null
    case 'docker':
    default:
      return item.split(/\s+/).length > 2 ? 'image[:tag|@digest] [platforms]' : null
  }
}

/** Field errors for `proxy.prefetch` of a repository write (format: the repository's). */
export function validatePrefetch(body: RepositoryWrite, format: string): FieldError[] {
  const config = body.proxy?.prefetch
  if (!config) return []
  const errors: FieldError[] = []
  const supported = PREFETCH_FORMATS.includes(format)
  if (!supported && config.refresh_popular) errors.push({ field: 'proxy.prefetch.refresh_popular', message: `${format} repositories cannot refresh popular metadata`, code: 'unsupported' })
  const warm = config.warm ?? []
  if (!supported && warm.length) errors.push({ field: 'proxy.prefetch.warm', message: `${format} repositories cannot warm items`, code: 'unsupported' })
  if (warm.length > 200) errors.push({ field: 'proxy.prefetch.warm', message: 'at most 200 items', code: 'too_long' })
  if (supported) {
    warm.forEach((item, i) => {
      const message = itemError(format, item)
      if (message) errors.push({ field: `proxy.prefetch.warm.${i}`, message, code: 'value_error' })
    })
  }
  const schedule = config.schedule?.trim().replace(/\s+/g, ' ')
  if (schedule) {
    const every = EVERY.exec(schedule)
    let message: string | null = null
    if (every) {
      const seconds = Number(every[1]) * { s: 1, m: 60, h: 3600 }[every[2]!.toLowerCase() as 's' | 'm' | 'h']
      if (seconds < 15 * 60) message = 'not a valid schedule: a warm list runs at most every 15 minutes'
    } else if (schedule.startsWith('@')) {
      if (!MACROS.includes(schedule.toLowerCase())) message = `not a valid schedule: unknown schedule '${schedule}'`
    } else if (schedule.split(' ').length !== 5) message = 'not a valid schedule: a cron expression has 5 fields: minute hour day-of-month month day-of-week'
    if (message) errors.push({ field: 'proxy.prefetch.schedule', message, code: 'value_error' })
  }
  return errors
}

/** The stored prefetch of a written proxy: the defaults when the body has none. */
export function normalisePrefetch(config: PrefetchConfig | undefined): PrefetchConfig {
  if (!config) return structuredClone(DEFAULT_PREFETCH)
  return { refresh_popular: Boolean(config.refresh_popular), warm: [...(config.warm ?? [])], schedule: config.schedule?.trim().replace(/\s+/g, ' ') || null }
}

// ---------------------------------------------------------------- handlers

export function prefetchHandlers(h: MockHelpers) {
  const find = (name: unknown) => world.repos.find((r) => r.name === String(name))
  const notProxy = (repo: Repository) => h.problem(409, 'Conflict', `${repo.name} is a ${repo.type} repository: only proxy repositories prefetch`)
  return [
    http.get(`${API}/repositories/:name/prefetch`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const repo = find(params.name)
      if (!repo) return h.notFound()
      if (repo.type !== 'proxy') return notProxy(repo)
      return HttpResponse.json(prefetchStatus(repo))
    }),
    http.post(`${API}/repositories/:name/prefetch/run`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const repo = find(params.name)
      if (!repo) return h.notFound()
      if (repo.type !== 'proxy') return notProxy(repo)
      if (!PREFETCH_FORMATS.includes(repo.format) || !(prefetchOf(repo).warm ?? []).length) {
        return h.problem(409, 'Conflict', `${repo.name} has no warm list: add items in its prefetch settings first`)
      }
      return HttpResponse.json(startWarm(repo), { status: 202 })
    }),
  ]
}
