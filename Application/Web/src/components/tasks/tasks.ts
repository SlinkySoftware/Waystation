// SPDX-License-Identifier: GPL-3.0-or-later
// Tasks page helpers (UI_DESIGN §4.9): labels for job kinds, schedules, worker health, and applying live job
// events to a run list without refetching.
import type { JobLogEvent, JobProgressEvent, JobRun, JobRunSummary, JobUpdateEvent, ScheduledTask, Worker } from '@/api/types'
import { describeSchedule } from '@/components/cleanup/cleanup'
import { type MessageKey, t } from '@/i18n'
import { formatBytes, formatCount, formatDuration } from '@/utils/format'
import { iconError, iconOk, iconWarning } from '@/utils/icons'

const KNOWN_KINDS = ['stats.rollup', 'blob.gc', 'cleanup.run'] as const
type KnownKind = (typeof KNOWN_KINDS)[number]
type KindKey = 'stats_rollup' | 'blob_gc' | 'cleanup_run'

const isKnown = (kind: string): kind is KnownKind => (KNOWN_KINDS as readonly string[]).includes(kind)
const kindKey = (kind: KnownKind): KindKey => kind.replace('.', '_') as KindKey

/** A job kind in words ("Blob garbage collection"); unknown kinds (newer workers) show as-is. */
export function kindLabel(kind: string): string {
  return isKnown(kind) ? t(`tasks.kind.${kindKey(kind)}`) : kind
}

export function kindHelp(kind: string): string | null {
  return isKnown(kind) ? t(`tasks.kindHelp.${kindKey(kind)}`) : null
}

/** A scheduled task in words: a cleanup policy's task is named after the policy. */
export function taskLabel(task: Pick<ScheduledTask, 'kind' | 'owner'>): string {
  if (task.owner?.kind === 'cleanup_policy') return t('tasks.scheduled.ownedBy', { name: task.owner.name ?? task.owner.id })
  return kindLabel(task.kind)
}

export function queueLabel(queue: string): string {
  const key = `tasks.queue.${queue}` as MessageKey
  const text = t(key)
  return text === key ? queue : text
}

const EVERY = /^@every\s+(\d+)\s*([smh])$/i

/** Plain-language schedule, adding `@every` to the cleanup page's reading ("Every 10 seconds"). */
export function describeTaskSchedule(schedule: string): string {
  const every = EVERY.exec(schedule.trim())
  if (every) {
    const unit = every[2]!.toLowerCase() as 's' | 'm' | 'h'
    return t('tasks.schedule.every', { n: Number(every[1]), unit: t(`tasks.schedule.unit.${unit}`) })
  }
  return describeSchedule(schedule)
}

const MACROS = new Set(['@yearly', '@annually', '@monthly', '@weekly', '@daily', '@midnight', '@hourly'])
const CRON_FIELD = /^(\*|[0-9a-z]+(-[0-9a-z]+)?)(\/\d+)?(,(\*|[0-9a-z]+(-[0-9a-z]+)?)(\/\d+)?)*$/i

/** Quick client-side check of a schedule (the server validates for real); null when it looks valid. */
export function scheduleError(schedule: string): string | null {
  const text = schedule.trim().replace(/\s+/g, ' ')
  if (!text) return t('tasks.schedule.required')
  const every = EVERY.exec(text)
  if (every) {
    const seconds = Number(every[1]) * { s: 1, m: 60, h: 3600 }[every[2]!.toLowerCase() as 's' | 'm' | 'h']
    return seconds < 10 ? t('tasks.schedule.tooOften') : null
  }
  if (text.startsWith('@')) return MACROS.has(text.toLowerCase()) ? null : t('cleanup.schedule.badMacro')
  const fields = text.split(' ')
  if (fields.length !== 5) return t('cleanup.schedule.fiveFields')
  return fields.every((f) => CRON_FIELD.test(f)) ? null : t('cleanup.schedule.badField')
}

export const SCHEDULE_PRESETS: readonly string[] = ['@every 10s', '@every 5m', '@hourly', '0 */6 * * *', '0 3 * * *', '0 3 * * 0']

// ---------------------------------------------------------------- workers

export interface Meta {
  label: string
  icon: string
  tone: string
  description: string
}

export function workerStateMeta(state: Worker['state']): Meta {
  switch (state) {
    case 'ok':
      return { label: t('tasks.workers.state.ok'), icon: iconOk, tone: 'tone-ok', description: t('tasks.workers.stateHelp.ok') }
    case 'late':
      return { label: t('tasks.workers.state.late'), icon: iconWarning, tone: 'tone-degraded', description: t('tasks.workers.stateHelp.late') }
    case 'lost':
    default:
      return { label: t('tasks.workers.state.lost'), icon: iconError, tone: 'tone-blocked', description: t('tasks.workers.stateHelp.lost') }
  }
}

/** Seconds since the worker's heartbeat, counted on from when the overview was loaded. */
export function heartbeatAge(worker: Pick<Worker, 'heartbeat_age_s'>, loadedAt: number, now: number): number {
  return worker.heartbeat_age_s + Math.max(0, (now - loadedAt) / 1000)
}

export function formatAge(seconds: number): string {
  return seconds < 60 ? `${Math.round(seconds)} s` : formatDuration(seconds)
}

// ---------------------------------------------------------------- live updates

/**
 * Apply a `job.update` / `job.progress` to a run list in place. Returns true when a row changed; false when the run
 * is not in the list (a new run: the caller refetches the first page).
 */
export function applyRunEvent(rows: JobRunSummary[], event: JobUpdateEvent | JobProgressEvent): boolean {
  const row = rows.find((r) => r.id === event.run_id)
  if (!row) return false
  if (event.type === 'job.progress') {
    if (row.status !== 'running') return true
    row.progress = { done: event.done, total: event.total ?? null, message: event.message ?? null }
    return true
  }
  row.status = event.status
  if (event.status !== 'running') {
    row.finished_at = event.ts
    row.duration_ms = Math.max(0, Date.parse(event.ts) - Date.parse(row.started_at))
  }
  if (event.error !== undefined) row.error = event.error
  return true
}

/** Append live log lines to an open run (the server keeps the last 500). */
export function appendLog(run: JobRun, event: JobLogEvent, keep = 500): void {
  if (event.run_id !== run.id) return
  run.log = [...run.log, ...event.lines].slice(-keep)
}

/** Result figures as label/value pairs for the run drawer (nested values are shown as JSON). */
export function resultEntries(result: Record<string, unknown>): { key: string; value: string }[] {
  return Object.entries(result).map(([key, value]) => ({
    key: key.replace(/_/g, ' '),
    value: typeof value === 'string' ? value : JSON.stringify(value),
  }))
}

export type TasksTab = 'scheduled' | 'runs' | 'workers' | 'dead'
export const TASKS_TABS: readonly TasksTab[] = ['scheduled', 'runs', 'workers', 'dead']

export function triggerLabel(trigger: JobRunSummary['trigger']): string {
  return t(`tasks.trigger.${trigger}`)
}

/** What a finished run reported, in a few words (the runs table's progress column). */
export function runSummary(run: Pick<JobRunSummary, 'kind' | 'result'>): string {
  const r = run.result
  const num = (key: string) => (typeof r[key] === 'number' ? r[key] : 0)
  if (run.kind === 'blob.gc') return t('tasks.runs.summary.gc', { count: num('freed_blobs'), n: formatCount(num('freed_blobs')), size: formatBytes(num('freed_bytes')) })
  if (run.kind === 'cleanup.run') return t('tasks.runs.summary.cleanup', { count: num('removed'), n: formatCount(num('removed')), size: formatBytes(num('bytes')) })
  return t('tasks.runs.summary.done')
}
