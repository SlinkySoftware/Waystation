// SPDX-License-Identifier: GPL-3.0-or-later
// Display metadata for domain values: formats, cache states, statuses. One vocabulary everywhere (SPEC §4.7).
import type { CacheState, Format, Repository, RepositoryStatus, RepositoryType } from '@/api/types'
import type { BrandMark } from '@/components/brandMarks'
import { t } from '@/i18n'

import {
  iconBlocked,
  iconCoalesced,
  iconError,
  iconFetching,
  iconHit,
  iconOffline,
  iconOk,
  iconPackage,
  iconProxy,
  iconRepositories,
  iconRevalidated,
  iconStale,
  iconWarning,
} from './icons'

const FORMAT_LABELS: Record<Format, string> = {
  docker: 'Docker',
  npm: 'npm',
  apt: 'apt',
  raw: 'Raw',
  yum: 'yum',
  pypi: 'PyPI',
  maven: 'Maven',
  go: 'Go',
  helm: 'Helm',
  terraform: 'Terraform',
  galaxy: 'Ansible Galaxy',
  swift: 'Swift',
  nuget: 'NuGet',
  cargo: 'Cargo',
  rubygems: 'RubyGems',
  composer: 'Composer',
  conda: 'Conda',
  apk: 'Alpine apk',
  huggingface: 'Hugging Face',
}

const FORMAT_MARKS: Record<Format, BrandMark> = {
  docker: 'docker',
  npm: 'npm',
  apt: 'debian',
  raw: 'raw',
  yum: 'redhat',
  pypi: 'pypi',
  maven: 'maven',
  go: 'go',
  helm: 'helm',
  terraform: 'terraform',
  galaxy: 'ansible',
  swift: 'swift',
  nuget: 'nuget',
  cargo: 'cargo',
  rubygems: 'rubygems',
  composer: 'composer',
  conda: 'conda',
  apk: 'alpine',
  huggingface: 'huggingface',
}

/** Chart colour slot per format: fixed, so a format keeps its colour in every chart (never by rank). */
export const FORMAT_CHART_SLOT: Partial<Record<Format, number>> = { docker: 1, npm: 2, apt: 3, raw: 4, yum: 5, pypi: 6, maven: 7, go: 8 }

export function formatLabel(format: Format): string {
  return FORMAT_LABELS[format] ?? format
}

export function formatColor(format: string): string {
  const slot = FORMAT_CHART_SLOT[format as Format]
  return slot ? `var(--chart-${slot})` : 'var(--text-tertiary)'
}

/** Formats offered in list and live filters: the creatable formats plus yum (displayed, not yet creatable). */
export const LISTED_FORMATS: readonly Format[] = ['docker', 'npm', 'apt', 'yum', 'raw', 'helm']

/** The mark for a format, refined by upstream for apt (Debian / Ubuntu / Raspberry Pi) and yum (Rocky / Alma / Fedora). */
export function formatMark(format: Format, upstreamUrl?: string | null): BrandMark {
  if (format === 'apt' && upstreamUrl) {
    if (/ubuntu/i.test(upstreamUrl)) return 'ubuntu'
    if (/raspberrypi|raspbian/i.test(upstreamUrl)) return 'raspberrypi'
  }
  if (format === 'yum' && upstreamUrl) {
    if (/rockylinux/i.test(upstreamUrl)) return 'rockylinux'
    if (/almalinux/i.test(upstreamUrl)) return 'almalinux'
    if (/fedoraproject|epel/i.test(upstreamUrl)) return 'fedora'
  }
  return FORMAT_MARKS[format] ?? 'raw'
}

export function repoMark(repo: Pick<Repository, 'format' | 'proxy'>): BrandMark {
  return formatMark(repo.format, repo.proxy?.upstream_url)
}

export interface Meta {
  label: string
  icon: string
  /** CSS class carrying colour (app.css tokens). */
  tone: string
  description: string
}

export function cacheMeta(state: CacheState): Meta {
  switch (state) {
    case 'HIT':
      return { label: 'HIT', icon: iconHit, tone: 'tone-hit', description: t('cache.HIT') }
    case 'FETCHING':
      return { label: 'FETCHING', icon: iconFetching, tone: 'tone-miss', description: t('cache.FETCHING') }
    case 'COALESCED':
      return { label: 'COALESCED', icon: iconCoalesced, tone: 'tone-miss-outline', description: t('cache.COALESCED') }
    case 'STALE':
      return { label: 'STALE', icon: iconStale, tone: 'tone-stale', description: t('cache.STALE') }
    case 'REVALIDATED':
      return { label: 'REVALIDATED', icon: iconRevalidated, tone: 'tone-hit-outline', description: t('cache.REVALIDATED') }
    case 'ERROR':
    default:
      return { label: 'ERROR', icon: iconError, tone: 'tone-error', description: t('cache.ERROR') }
  }
}

export function statusMeta(status: RepositoryStatus): Meta {
  switch (status) {
    case 'ok':
      return { label: t('status.ok'), icon: iconOk, tone: 'tone-ok', description: t('status.okHelp') }
    case 'degraded':
      return { label: t('status.degraded'), icon: iconWarning, tone: 'tone-degraded', description: t('status.degradedHelp') }
    case 'blocked':
      return { label: t('status.blocked'), icon: iconBlocked, tone: 'tone-blocked', description: t('status.blockedHelp') }
    case 'offline':
    default:
      return { label: t('status.offline'), icon: iconOffline, tone: 'tone-offline', description: t('status.offlineHelp') }
  }
}

export function typeMeta(type: RepositoryType): { label: string; icon: string } {
  switch (type) {
    case 'proxy':
      return { label: t('repoType.proxy'), icon: iconProxy }
    case 'hosted':
      return { label: t('repoType.hosted'), icon: iconPackage }
    case 'group':
    default:
      return { label: t('repoType.group'), icon: iconRepositories }
  }
}
