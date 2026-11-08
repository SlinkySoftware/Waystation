// SPDX-License-Identifier: GPL-3.0-or-later
// Command palette model (UI_DESIGN §3): the items it offers and how a query ranks them. Pure, so it is unit-tested.
import type { RouteLocationRaw } from 'vue-router'

import type { Repository, SearchHit } from '@/api/types'
import type { BrandMark } from '@/components/brandMarks'
import { type MessageKey, t } from '@/i18n'
import type { NavGroup } from '@/layouts/navigation'
import type { RecentItem } from '@/stores/recent'
import { formatLabel, formatMark, repoMark } from '@/utils/domain'
import { iconAdd, iconRepositories, iconSearch, iconSystemTheme } from '@/utils/icons'
import { browseLocation, searchLocation } from '@/utils/search'

export type PaletteGroup = 'search' | 'recent' | 'repositories' | 'pages' | 'results' | 'actions'

export interface PaletteItem {
  id: string
  group: PaletteGroup
  label: string
  detail?: string
  hint?: string
  icon?: string
  mark?: BrandMark
  to?: RouteLocationRaw
  /** Client-side command instead of a navigation. */
  command?: 'theme'
  /** What to remember in "Recent" when chosen. */
  remember?: Omit<RecentItem, 'at'>
  /** Extra words the query may match (format, type...). */
  keywords?: string
}

export interface PaletteSection {
  group: PaletteGroup
  label: string
  items: PaletteItem[]
}

export const GROUP_LABELS: Record<PaletteGroup, MessageKey> = {
  search: 'palette.groups.search',
  recent: 'palette.groups.recent',
  repositories: 'palette.groups.repositories',
  pages: 'palette.groups.pages',
  results: 'palette.groups.results',
  actions: 'palette.groups.actions',
}

/** 3: the text starts with the query, 2: a word of it does, 1: it contains the query, 0: no match. */
export function score(text: string, query: string): number {
  const q = query.trim().toLowerCase()
  if (!q) return 1
  const s = text.toLowerCase()
  if (s.startsWith(q)) return 3
  if (s.split(/[\s/@._:-]+/).some((w) => w.startsWith(q))) return 2
  return s.includes(q) ? 1 : 0
}

/** Items matching `query`, best first (stable within a score), at most `max`. */
export function rank(items: PaletteItem[], query: string, max: number): PaletteItem[] {
  return items
    .map((item, i) => ({ item, i, s: Math.max(score(item.label, query), item.keywords ? Math.min(2, score(item.keywords, query)) : 0) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s || a.i - b.i)
    .slice(0, max)
    .map((x) => x.item)
}

export function pageItems(navigation: NavGroup[], can: (capability: string) => boolean): PaletteItem[] {
  return navigation
    .flatMap((g) => g.items)
    .filter((i) => !i.planned && (!i.capability || can(i.capability)))
    .map((i) => ({ id: `page:${i.name}`, group: 'pages', label: t(i.label), icon: i.icon, to: i.to, hint: t('palette.page') }))
}

export function actionItems(can: (capability: string) => boolean): PaletteItem[] {
  const out: PaletteItem[] = []
  if (can('repositories.create')) {
    out.push({ id: 'action:create', group: 'actions', label: t('palette.createRepository'), icon: iconAdd, to: { name: 'repositories', query: { create: '1' } }, keywords: 'new add proxy' })
  }
  out.push({ id: 'action:theme', group: 'actions', label: t('palette.switchTheme'), icon: iconSystemTheme, command: 'theme', keywords: 'dark light mode appearance' })
  return out
}

export function repositoryItem(repo: Repository): PaletteItem {
  const to: RecentItem['to'] = { name: 'repository', params: { name: repo.name } }
  return {
    id: `repo:${repo.name}`,
    group: 'repositories',
    label: repo.name,
    detail: `${formatLabel(repo.format)} · ${t(`repoType.${repo.type}`)}`,
    mark: repoMark(repo),
    to,
    keywords: `${repo.format} ${formatLabel(repo.format)} ${repo.type} ${repo.description ?? ''}`,
    remember: { id: `repo:${repo.name}`, kind: 'repository', label: repo.name, format: repo.format, repository: repo.name, to },
  }
}

export function resultItem(hit: SearchHit, mark: BrandMark): PaletteItem {
  const label = hit.version ? `${hit.name} ${hit.version}` : hit.name
  const to = browseLocation(hit) as RecentItem['to']
  const id = `hit:${hit.repository}:${hit.kind}:${hit.name}:${hit.version ?? ''}:${hit.path ?? ''}:${hit.qualifier ?? ''}`
  return {
    id,
    group: 'results',
    label,
    detail: [hit.repository, hit.qualifier ?? hit.path].filter(Boolean).join(' · '),
    hint: t(`search.kinds.${hit.kind}`),
    mark,
    to,
    remember: { id, kind: 'hit', label, detail: hit.repository, format: hit.format, repository: hit.repository, to },
  }
}

export function searchItem(query: string): PaletteItem {
  const q = query.trim()
  const to = searchLocation(q) as RecentItem['to']
  return {
    id: 'search:all',
    group: 'search',
    label: t('palette.searchFor', { q }),
    icon: iconSearch,
    to,
    hint: t('palette.allResults'),
    remember: { id: `query:${q.toLowerCase()}`, kind: 'query', label: q, to },
  }
}

export function recentItem(item: RecentItem): PaletteItem {
  const icon = item.kind === 'query' ? iconSearch : iconRepositories
  return {
    id: `recent:${item.id}`,
    group: 'recent',
    label: item.label,
    detail: item.kind === 'query' ? t('palette.recentSearch') : item.detail,
    mark: item.format && item.kind !== 'query' ? formatMark(item.format) : undefined,
    icon,
    to: item.to,
    remember: { id: item.id, kind: item.kind, label: item.label, detail: item.detail, format: item.format, repository: item.repository, to: item.to },
  }
}

/** Group items into sections in display order, dropping empty ones. */
export function toSections(items: PaletteItem[]): PaletteSection[] {
  const order: PaletteGroup[] = ['search', 'recent', 'repositories', 'pages', 'results', 'actions']
  return order
    .map((group) => ({ group, label: t(GROUP_LABELS[group]), items: items.filter((i) => i.group === group) }))
    .filter((s) => s.items.length > 0)
}

/** The next active item when moving by `delta` (wrapping), or jumping to the start/end. */
export function moveActive(items: PaletteItem[], activeId: string | null, delta: number | 'start' | 'end'): string | null {
  if (!items.length) return null
  if (delta === 'start') return items[0]!.id
  if (delta === 'end') return items[items.length - 1]!.id
  const at = items.findIndex((i) => i.id === activeId)
  const next = at < 0 ? (delta > 0 ? 0 : items.length - 1) : (at + delta + items.length) % items.length
  return items[next]!.id
}
