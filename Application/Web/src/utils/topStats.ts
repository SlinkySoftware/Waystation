// SPDX-License-Identifier: GPL-3.0-or-later
// Helpers for the top-artifacts and storage-growth views (dashboard and Storage page).
import type { RouteLocationRaw } from 'vue-router'

import type { StorageHistory, TopArtifact } from '@/api/types'

/** Where the repository Browse tab shows this artifact (format-aware views where they take a query). */
export function artifactLocation(a: Pick<TopArtifact, 'repository' | 'format' | 'name' | 'version' | 'path'>): RouteLocationRaw {
  const query: Record<string, string> = {}
  if (a.format === 'docker') {
    query.image = a.name
    if (a.version) query.ref = a.version
  } else if (a.format === 'npm') {
    query.package = a.name
  } else if (a.format === 'yum') {
    query.view = 'packages'
    query.package = a.name
  } else if (a.format === 'apt') {
    query.q = a.name
  } else if (a.path && a.path.includes('/')) {
    query.path = a.path.slice(0, a.path.lastIndexOf('/'))
  }
  return { name: 'repository', params: { name: a.repository, tab: 'browse' }, query }
}

export interface GrowthSeries {
  /** Bytes on disk (after de-duplication). */
  used: number[]
  /** Bytes saved by de-duplication (stored - used, never negative). */
  saved: number[]
  /** Whether any bucket holds a snapshot. */
  empty: boolean
}

/**
 * Chart values from the snapshot history: buckets without a snapshot after the first one repeat the previous value
 * (a missed hour is not a drop to zero); buckets before the first snapshot stay 0.
 */
export function growthSeries(h: Pick<StorageHistory, 'used_bytes' | 'stored_bytes'>): GrowthSeries {
  const used: number[] = []
  const saved: number[] = []
  let lastUsed: number | null = null
  let lastStored: number | null = null
  for (let i = 0; i < h.used_bytes.length; i++) {
    lastUsed = h.used_bytes[i] ?? lastUsed
    lastStored = h.stored_bytes[i] ?? lastStored
    used.push(lastUsed ?? 0)
    saved.push(lastUsed !== null && lastStored !== null ? Math.max(0, lastStored - lastUsed) : 0)
  }
  return { used, saved, empty: !h.used_bytes.some((v) => v !== null) && !h.stored_bytes.some((v) => v !== null) }
}

/** Growth over the window: last value minus the first snapshot in it (null without two snapshots). */
export function growthDelta(values: (number | null)[]): number | null {
  const present = values.filter((v): v is number => v !== null)
  if (present.length < 2) return null
  return present[present.length - 1]! - present[0]!
}
