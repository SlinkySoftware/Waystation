// SPDX-License-Identifier: GPL-3.0-or-later
// Storage page figures (UI_DESIGN §4.7) derived from `GET /stats/storage`.
import type { Format, StorageSummary } from '@/api/types'

export interface VolumeBreakdown {
  /** Bytes held by Waystation blobs. */
  waystation: number
  /** Used on the volume by anything else. */
  other: number
  free: number
  total: number
  /** 0..1 of the volume. */
  usedShare: number
  freeShare: number
}

/** Split the data volume into Waystation blobs, other files and free space (each clamped, summing to `total`). */
export function volumeBreakdown(s: Pick<StorageSummary, 'used_bytes' | 'free_bytes' | 'total_bytes'>): VolumeBreakdown {
  const total = Math.max(0, s.total_bytes)
  const free = Math.min(Math.max(0, s.free_bytes), total)
  const waystation = Math.min(Math.max(0, s.used_bytes), total - free)
  const other = Math.max(0, total - free - waystation)
  return { waystation, other, free, total, usedShare: total ? (total - free) / total : 0, freeShare: total ? free / total : 0 }
}

export interface DedupFigures {
  /** What the stored artifacts would take without de-duplication. */
  logical: number
  saved: number
  /** 0..1 of `logical` saved. */
  share: number
}

export function dedupFigures(s: Pick<StorageSummary, 'used_bytes' | 'dedup_saved_bytes'>): DedupFigures {
  const saved = Math.max(0, s.dedup_saved_bytes)
  const logical = Math.max(0, s.used_bytes) + saved
  return { logical, saved, share: logical ? saved / logical : 0 }
}

export interface RepoStorageRow {
  repository: string
  format: Format
  bytes: number
  artifacts: number
  /** 0..1 of all referenced bytes. */
  share: number
  /** Mean artifact size, or null without artifacts. */
  average: number | null
}

export function repoRows(s: Pick<StorageSummary, 'by_repository'>): RepoStorageRow[] {
  const total = s.by_repository.reduce((a, r) => a + Math.max(0, r.bytes), 0)
  return s.by_repository.map((r) => ({
    repository: r.repository,
    format: r.format,
    bytes: r.bytes,
    artifacts: r.artifacts,
    share: total ? Math.max(0, r.bytes) / total : 0,
    average: r.artifacts > 0 ? r.bytes / r.artifacts : null,
  }))
}

/** Case-insensitive filter on repository name or format. */
export function filterRows(rows: RepoStorageRow[], needle: string | null): RepoStorageRow[] {
  const q = needle?.trim().toLowerCase()
  if (!q) return rows
  return rows.filter((r) => r.repository.toLowerCase().includes(q) || r.format.includes(q))
}

/** The `count` largest repositories with any bytes, largest first. */
export function largest(rows: RepoStorageRow[], count: number): RepoStorageRow[] {
  return rows
    .filter((r) => r.bytes > 0)
    .sort((a, b) => b.bytes - a.bytes || a.repository.localeCompare(b.repository))
    .slice(0, count)
}
