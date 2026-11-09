// SPDX-License-Identifier: GPL-3.0-or-later
// Repository quotas (SPECIFICATION §4.8): display metadata for usage against a quota, and the size <-> bytes mapping
// the repository form uses.
import type { QuotaMode, RepositoryQuota } from '@/api/types'
import { t } from '@/i18n'

import { formatBytes, formatPercent } from './format'
import { iconError, iconStorage, iconWarning } from './icons'

export type QuotaState = RepositoryQuota['state']
export type QuotaUnit = 'MiB' | 'GiB' | 'TiB'

export const QUOTA_UNITS: readonly QuotaUnit[] = ['MiB', 'GiB', 'TiB']
const UNIT_BYTES: Record<QuotaUnit, number> = { MiB: 1024 ** 2, GiB: 1024 ** 3, TiB: 1024 ** 4 }

export interface QuotaMeta {
  state: QuotaState
  /** Badge tone class (never the only signal: icon and label carry the state too). */
  tone: string
  icon: string
  label: string
  /** "1.2 GiB of 5.0 GiB" */
  usage: string
  percent: string
  /** Meter fill, clamped to 0-1. */
  fill: number
  description: string
}

export function quotaMeta(q: RepositoryQuota): QuotaMeta {
  const state = q.state
  const tone = state === 'exceeded' ? 'tone-blocked' : state === 'warning' ? 'tone-degraded' : 'tone-ok'
  const icon = state === 'exceeded' ? iconError : state === 'warning' ? iconWarning : iconStorage
  const description =
    state === 'exceeded'
      ? q.mode === 'hard'
        ? t('quota.exceededHard')
        : t('quota.exceededSoft')
      : state === 'warning'
        ? t('quota.warningText')
        : t('quota.okText')
  return {
    state,
    tone,
    icon,
    label: t(`quota.state.${state}`),
    usage: t('quota.usage', { used: formatBytes(q.used_bytes), quota: formatBytes(q.quota_bytes) }),
    percent: formatPercent(q.used_ratio, 0),
    fill: Math.max(0, Math.min(1, q.used_ratio)),
    description,
  }
}

export function quotaModeLabel(mode: QuotaMode): string {
  return t(`quota.mode.${mode}`)
}

/** Bytes for a size in the form (whole bytes, at least 1). */
export function toBytes(size: number, unit: QuotaUnit): number {
  return Math.max(1, Math.round(size * UNIT_BYTES[unit]))
}

/** The largest unit that shows `bytes` as a size of at least 1 (to 2 decimals). */
export function fromBytes(bytes: number): { size: number; unit: QuotaUnit } {
  const unit = [...QUOTA_UNITS].reverse().find((u) => bytes >= UNIT_BYTES[u]) ?? 'MiB'
  return { size: Math.round((bytes / UNIT_BYTES[unit]) * 100) / 100, unit }
}
