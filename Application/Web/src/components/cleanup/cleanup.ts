// SPDX-License-Identifier: GPL-3.0-or-later
// Cleanup policy helpers (UI_DESIGN §4.8): criteria summaries, the "at least one selecting criterion" rule,
// schedule presets and descriptions, run status display, format compatibility. Pure, so they are unit tested.
import type { CleanupCriteria, CleanupFormat, CleanupPolicy, CleanupPolicyWrite, CleanupRunSummary, Format, GcRun } from '@/api/types'
import { t } from '@/i18n'
import { formatLabel, type Meta } from '@/utils/domain'
import { formatBytes, formatCount } from '@/utils/format'
import { iconBlocked, iconError, iconOk, iconSync, iconWarning } from '@/utils/icons'

export type DayCriterion = 'last_downloaded_older_than_days' | 'published_older_than_days' | 'untagged_older_than_days'
export type NumberCriterion = DayCriterion | 'keep_latest_n_versions'
export type RegexCriterion = 'name_include' | 'name_exclude' | 'version_include' | 'version_exclude'

export const NUMBER_CRITERIA: readonly NumberCriterion[] = ['last_downloaded_older_than_days', 'published_older_than_days', 'keep_latest_n_versions', 'untagged_older_than_days']
export const REGEX_CRITERIA: readonly RegexCriterion[] = ['name_include', 'name_exclude', 'version_include', 'version_exclude']

/** Value a numeric criterion starts at when switched on, and its bounds (CleanupCriteria in the contract). */
export const NUMBER_LIMITS: Record<NumberCriterion, { initial: number; max: number }> = {
  last_downloaded_older_than_days: { initial: 90, max: 36500 },
  published_older_than_days: { initial: 180, max: 36500 },
  keep_latest_n_versions: { initial: 5, max: 1000 },
  untagged_older_than_days: { initial: 14, max: 36500 },
}

export function emptyCriteria(): CleanupCriteria {
  return {
    last_downloaded_older_than_days: null,
    published_older_than_days: null,
    keep_latest_n_versions: null,
    name_include: null,
    name_exclude: null,
    version_include: null,
    version_exclude: null,
    prerelease_only: false,
    untagged_older_than_days: null,
  }
}

/** The docker-only criterion applies to docker policies and to `*` (where it only touches docker repositories). */
export function supportsUntagged(format: CleanupFormat): boolean {
  return format === 'docker' || format === '*'
}

const blank = (v: string | null | undefined) => !v || !v.trim()

/** Criteria as sent to the server: empty strings become null, the docker-only criterion is dropped elsewhere. */
export function normaliseCriteria(criteria: CleanupCriteria, format: CleanupFormat): CleanupCriteria {
  const out: CleanupCriteria = { ...emptyCriteria(), prerelease_only: Boolean(criteria.prerelease_only) }
  for (const key of NUMBER_CRITERIA) {
    const value = criteria[key]
    out[key] = typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : null
  }
  for (const key of REGEX_CRITERIA) out[key] = blank(criteria[key]) ? null : criteria[key]!.trim()
  if (!supportsUntagged(format)) out.untagged_older_than_days = null
  return out
}

/** At least one selecting criterion (age, keep-latest-N, an include pattern, pre-release, untagged) must be set. */
export function hasSelector(criteria: CleanupCriteria): boolean {
  return (
    criteria.last_downloaded_older_than_days != null ||
    criteria.published_older_than_days != null ||
    criteria.keep_latest_n_versions != null ||
    !blank(criteria.name_include) ||
    !blank(criteria.version_include) ||
    Boolean(criteria.prerelease_only) ||
    criteria.untagged_older_than_days != null
  )
}

/** A best-effort syntax check (the server's regex dialect decides). */
export function isValidRegex(pattern: string | null | undefined): boolean {
  if (blank(pattern)) return true
  try {
    new RegExp(pattern!)
    return true
  } catch {
    return false
  }
}

export interface CriteriaChip {
  key: keyof CleanupCriteria
  label: string
  /** Protecting criteria (exclusions, keep-latest) are drawn differently from selecting ones. */
  protects: boolean
}

/** Short chips describing a policy's criteria, in a stable order. */
export function criteriaChips(criteria: CleanupCriteria): CriteriaChip[] {
  const chips: CriteriaChip[] = []
  const add = (key: keyof CleanupCriteria, label: string, protects = false) => chips.push({ key, label, protects })
  if (criteria.last_downloaded_older_than_days != null) add('last_downloaded_older_than_days', t('cleanup.chip.unused', { days: criteria.last_downloaded_older_than_days }))
  if (criteria.published_older_than_days != null) add('published_older_than_days', t('cleanup.chip.older', { days: criteria.published_older_than_days }))
  if (criteria.untagged_older_than_days != null) add('untagged_older_than_days', t('cleanup.chip.untagged', { days: criteria.untagged_older_than_days }))
  if (criteria.prerelease_only) add('prerelease_only', t('cleanup.chip.prerelease'))
  if (!blank(criteria.name_include)) add('name_include', t('cleanup.chip.nameInclude', { re: criteria.name_include! }))
  if (!blank(criteria.version_include)) add('version_include', t('cleanup.chip.versionInclude', { re: criteria.version_include! }))
  if (criteria.keep_latest_n_versions != null) add('keep_latest_n_versions', t('cleanup.chip.keep', { count: criteria.keep_latest_n_versions }), true)
  if (!blank(criteria.name_exclude)) add('name_exclude', t('cleanup.chip.nameExclude', { re: criteria.name_exclude! }), true)
  if (!blank(criteria.version_exclude)) add('version_exclude', t('cleanup.chip.versionExclude', { re: criteria.version_exclude! }), true)
  return chips
}

// ---------------------------------------------------------------- schedules

export type ScheduleMode = 'manual' | 'daily' | 'weekly' | 'custom'

/** Preset cron strings (UTC): quiet hours, cleanup before blob GC. */
export const SCHEDULE_PRESETS: Record<Exclude<ScheduleMode, 'manual' | 'custom'>, string> = {
  daily: '0 3 * * *',
  weekly: '0 3 * * 0',
}

export function scheduleMode(schedule: string | null | undefined): ScheduleMode {
  if (!schedule) return 'manual'
  if (schedule === SCHEDULE_PRESETS.daily) return 'daily'
  if (schedule === SCHEDULE_PRESETS.weekly) return 'weekly'
  return 'custom'
}

const MACROS = new Set(['@yearly', '@annually', '@monthly', '@weekly', '@daily', '@midnight', '@hourly'])
const CRON_FIELD = /^(\*|\d+(-\d+)?)(\/\d+)?(,(\*|\d+(-\d+)?)(\/\d+)?)*$/

/** Five-field cron or a macro; null when valid, else a message. The server parses it for real. */
export function cronError(schedule: string | null | undefined): string | null {
  const text = schedule?.trim() ?? ''
  if (!text) return t('cleanup.schedule.required')
  if (text.startsWith('@')) return MACROS.has(text) ? null : t('cleanup.schedule.badMacro')
  const fields = text.split(/\s+/)
  if (fields.length !== 5) return t('cleanup.schedule.fiveFields')
  return fields.every((f) => CRON_FIELD.test(f)) ? null : t('cleanup.schedule.badField')
}

const pad = (n: string) => n.padStart(2, '0')
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

/** Human description of a schedule: "Manual only", "Daily at 03:00 UTC", "Weekly on Sun at 03:00 UTC", else the cron. */
export function describeSchedule(schedule: string | null | undefined): string {
  if (!schedule) return t('cleanup.schedule.manualOnly')
  const macro: Record<string, string> = {
    '@hourly': t('cleanup.schedule.hourly'),
    '@daily': t('cleanup.schedule.dailyAt', { time: '00:00' }),
    '@midnight': t('cleanup.schedule.dailyAt', { time: '00:00' }),
    '@weekly': t('cleanup.schedule.weeklyAt', { day: 'Sun', time: '00:00' }),
    '@monthly': t('cleanup.schedule.monthlyAt', { day: 1, time: '00:00' }),
  }
  if (macro[schedule]) return macro[schedule]
  const f = schedule.trim().split(/\s+/)
  if (f.length === 5 && /^\d+$/.test(f[0]!) && /^\d+$/.test(f[1]!) && f[3] === '*') {
    const time = `${pad(f[1]!)}:${pad(f[0]!)}`
    if (f[2] === '*' && f[4] === '*') return t('cleanup.schedule.dailyAt', { time })
    if (f[2] === '*' && /^[0-7]$/.test(f[4]!)) return t('cleanup.schedule.weeklyAt', { day: WEEKDAYS[Number(f[4]) % 7]!, time })
    if (/^\d+$/.test(f[2]!) && f[4] === '*') return t('cleanup.schedule.monthlyAt', { day: Number(f[2]), time })
  }
  return t('cleanup.schedule.cron', { cron: schedule })
}

// ---------------------------------------------------------------- runs and policies

export type RunStatus = CleanupRunSummary['status']

export function runStatusMeta(status: RunStatus): Meta {
  switch (status) {
    case 'running':
      return { label: t('cleanup.run.running'), icon: iconSync, tone: 'tone-hit', description: t('cleanup.run.runningHelp') }
    case 'succeeded':
      return { label: t('cleanup.run.succeeded'), icon: iconOk, tone: 'tone-ok', description: t('cleanup.run.succeededHelp') }
    case 'interrupted':
      return { label: t('cleanup.run.interrupted'), icon: iconWarning, tone: 'tone-degraded', description: t('cleanup.run.interruptedHelp') }
    case 'dead':
      return { label: t('cleanup.run.dead'), icon: iconBlocked, tone: 'tone-blocked', description: t('cleanup.run.deadHelp') }
    case 'failed':
    default:
      return { label: t('cleanup.run.failed'), icon: iconError, tone: 'tone-blocked', description: t('cleanup.run.failedHelp') }
  }
}

/** Seconds a run took (or has been running, given `now`). */
export function runSeconds(run: Pick<CleanupRunSummary | GcRun, 'started_at' | 'finished_at'>, now: number = Date.now()): number {
  const end = run.finished_at ? Date.parse(run.finished_at) : now
  return Math.max(0, (end - Date.parse(run.started_at)) / 1000)
}

/** What a finished run removed, in words: "Nothing removed" or "12 items · 3.4 MiB removed". */
export function runOutcome(run: Pick<CleanupRunSummary, 'removed' | 'bytes'>): string {
  if (!run.removed && !run.bytes) return t('cleanup.nothingRemoved')
  return t('cleanup.removedSummary', { count: run.removed, n: formatCount(run.removed), size: formatBytes(run.bytes) })
}

/** 0..1 progress, or null when the total is unknown (indeterminate bar). */
export function progressRatio(progress: { done: number; total: number | null }): number | null {
  if (!progress.total) return null
  return Math.min(1, Math.max(0, progress.done / progress.total))
}

export function formatScope(format: CleanupFormat): string {
  return format === '*' ? t('cleanup.allFormats') : formatLabel(format)
}

/** A policy can be attached to a repository when it is for that repository's format or for every format. */
export function compatible(policy: Pick<CleanupPolicy, 'format'>, format: Format): boolean {
  return policy.format === '*' || policy.format === format
}

/** The write body for an existing policy with some fields changed (e.g. the enabled toggle). */
export function toWrite(policy: CleanupPolicy, patch: Partial<CleanupPolicyWrite> = {}): CleanupPolicyWrite {
  return {
    name: policy.name,
    description: policy.description,
    format: policy.format,
    criteria: normaliseCriteria(policy.criteria, policy.format),
    schedule: policy.schedule,
    enabled: policy.enabled,
    ...patch,
  }
}

/** Field errors for one criterion from a problem document's `errors[]` (keys like `criteria.keep_latest_n_versions`). */
export function criteriaError(errors: Record<string, string>, key: keyof CleanupCriteria): string | undefined {
  return errors[`criteria.${key}`]
}
