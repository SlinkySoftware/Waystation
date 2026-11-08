// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { dedupFigures, filterRows, largest, repoRows, volumeBreakdown } from './storageStats'

const GiB = 1024 ** 3

describe('volumeBreakdown', () => {
  it('splits the volume into blobs, other files and free space', () => {
    const v = volumeBreakdown({ used_bytes: 300 * GiB, free_bytes: 600 * GiB, total_bytes: 1000 * GiB })
    expect(v).toMatchObject({ waystation: 300 * GiB, other: 100 * GiB, free: 600 * GiB, total: 1000 * GiB })
    expect(v.usedShare).toBeCloseTo(0.4)
    expect(v.freeShare).toBeCloseTo(0.6)
  })

  it('clamps inconsistent figures (blobs larger than the used space, unknown volume)', () => {
    const v = volumeBreakdown({ used_bytes: 500, free_bytes: 800, total_bytes: 1000 })
    expect(v.waystation + v.other + v.free).toBe(1000)
    expect(v).toMatchObject({ waystation: 200, other: 0, free: 800 })
    expect(volumeBreakdown({ used_bytes: 5, free_bytes: 0, total_bytes: 0 })).toMatchObject({ waystation: 0, usedShare: 0, freeShare: 0 })
  })
})

describe('dedupFigures', () => {
  it('reports savings against what the artifacts would take without de-duplication', () => {
    expect(dedupFigures({ used_bytes: 900, dedup_saved_bytes: 100 })).toEqual({ logical: 1000, saved: 100, share: 0.1 })
    expect(dedupFigures({ used_bytes: 0, dedup_saved_bytes: 0 }).share).toBe(0)
  })
})

describe('repository rows', () => {
  const rows = repoRows({
    by_repository: [
      { repository: 'dockerhub-proxy', format: 'docker', bytes: 600, artifacts: 3 },
      { repository: 'npm', format: 'npm', bytes: 400, artifacts: 0 },
      { repository: 'empty', format: 'raw', bytes: 0, artifacts: 0 },
    ],
  })

  it('computes the share of the total and the mean artifact size', () => {
    expect(rows.map((r) => [r.repository, r.share, r.average])).toEqual([
      ['dockerhub-proxy', 0.6, 200],
      ['npm', 0.4, null],
      ['empty', 0, null],
    ])
  })

  it('filters by name or format and ranks the largest', () => {
    expect(filterRows(rows, ' DOCKER ').map((r) => r.repository)).toEqual(['dockerhub-proxy'])
    expect(filterRows(rows, 'npm').map((r) => r.repository)).toEqual(['npm'])
    expect(filterRows(rows, null)).toHaveLength(3)
    expect(largest(rows, 5).map((r) => r.repository)).toEqual(['dockerhub-proxy', 'npm'])
    expect(largest(rows, 1)).toHaveLength(1)
  })
})
