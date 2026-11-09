// SPDX-License-Identifier: GPL-3.0-or-later
// Import from Nexus (SPEC §4.12): the connection form model, request bodies and how plan items are shown.
import type { NexusApplyRequest, NexusImportItem, NexusImportReport, NexusImportRequest } from '@/api/types'
import { t } from '@/i18n'
import { iconAdd, iconError, iconInfo, iconOk, iconWarning } from '@/utils/icons'

export interface NexusFormModel {
  url: string
  username: string
  password: string
  allowPrivate: boolean
  caPem: string
  dockerAliases: boolean
  /** Nexus docker repository for unprefixed names; null = let the server detect it. */
  defaultDockerRepo: string | null
}

export function emptyModel(): NexusFormModel {
  return { url: '', username: 'admin', password: '', allowPrivate: false, caPem: '', dockerAliases: true, defaultDockerRepo: null }
}

export function toRequest(m: NexusFormModel): NexusImportRequest {
  return {
    url: m.url.trim(),
    username: m.username.trim(),
    password: m.password,
    allow_private: m.allowPrivate,
    ca_pem: m.caPem.trim() || null,
    docker_aliases: m.dockerAliases,
    default_docker_repo: m.defaultDockerRepo,
  }
}

export function toApply(m: NexusFormModel, selected: readonly string[]): NexusApplyRequest {
  return { ...toRequest(m), repositories: [...selected] }
}

export type Tone = 'positive' | 'info' | 'warning' | 'negative' | 'neutral'

const TONES: Record<Tone, { cls: string; icon: string }> = {
  positive: { cls: 'tone-ok', icon: iconOk },
  info: { cls: 'tone-hit', icon: iconAdd },
  warning: { cls: 'tone-degraded', icon: iconWarning },
  negative: { cls: 'tone-blocked', icon: iconError },
  neutral: { cls: 'tone-offline', icon: iconInfo },
}

/** What the row says (label, tone class, icon: never colour alone): the apply outcome once applied, else the plan. */
export function rowState(item: NexusImportItem): { label: string; tone: Tone; cls: string; icon: string } {
  let tone: Tone
  let label: string
  if (item.outcome) {
    const tones: Record<NonNullable<NexusImportItem['outcome']>, Tone> = {
      created: 'positive',
      exists: 'neutral',
      unsupported: 'neutral',
      invalid: 'warning',
      failed: 'negative',
      not_selected: 'neutral',
    }
    tone = tones[item.outcome]
    label = t(`nexusImport.outcome.${item.outcome}`)
  } else {
    const tones: Record<NexusImportItem['action'], Tone> = { create: 'info', exists: 'neutral', unsupported: 'neutral', invalid: 'warning' }
    tone = tones[item.action]
    label = t(`nexusImport.action.${item.action}`)
  }
  return { label, tone, ...TONES[tone] }
}

/** Repositories an apply would create: every `create` item, selected by default. */
export function creatable(report: NexusImportReport | null): string[] {
  return (report?.items ?? []).filter((i) => i.action === 'create').map((i) => i.name)
}

/** Docker proxies and groups the import creates: candidates for the default docker repository. */
export function dockerCandidates(report: NexusImportReport | null): string[] {
  return (report?.items ?? []).filter((i) => i.format === 'docker' && i.action === 'create' && i.type !== 'hosted').map((i) => i.name)
}

/** One line on where the repository points (upstream, members, or nothing for hosted). */
export function target(item: NexusImportItem): string {
  const repo = item.repository as (NexusImportItem['repository'] & { group?: { members?: string[] } }) | null
  if (repo?.proxy?.upstream_url) return repo.proxy.upstream_url
  if (repo?.group?.members?.length) return repo.group.members.join(', ')
  return ''
}

export function waystationKind(item: NexusImportItem): string {
  return item.format && item.type ? `${item.format} ${item.type}` : '—'
}
