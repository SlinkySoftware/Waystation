// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { StatsTickEvent, TimeSeries } from '@/api/types'

import { applyTick } from './useTraffic'

const series = (): TimeSeries => ({
  range: '15m',
  step_s: 10,
  unit: 'bytes',
  timestamps: ['2026-11-08T12:00:00.000Z', '2026-11-08T12:00:10.000Z'],
  series: [
    { key: 'hit', label: 'Hit', values: [100, 200] },
    { key: 'upstream', label: 'Upstream', values: [10, 20] },
  ],
})

const tick = (ts: string): StatsTickEvent => ({
  type: 'stats.tick',
  ts,
  requests_per_s: 10,
  bytes_out_per_s: 1000,
  bytes_upstream_per_s: 100,
  bytes_saved_per_s: 900,
  hit_ratio_1m: 0.9,
  active_transfers: 3,
})

describe('applyTick', () => {
  it('adds a tick to the newest bucket', () => {
    const out = applyTick(series(), tick('2026-11-08T12:00:15.000Z'), 'cache', [])
    expect(out.series[0]!.values).toEqual([100, 1100])
    expect(out.series[1]!.values).toEqual([10, 120])
    expect(out.timestamps).toHaveLength(2)
  })

  it('rolls to a new bucket when the step passes', () => {
    const out = applyTick(series(), tick('2026-11-08T12:00:21.000Z'), 'cache', [])
    expect(out.timestamps).toEqual(['2026-11-08T12:00:10.000Z', '2026-11-08T12:00:20.000Z'])
    expect(out.series[0]!.values).toEqual([200, 900])
  })

  it('splits requests by cache ratio', () => {
    const s = { ...series(), unit: 'requests' as const }
    const out = applyTick(s, tick('2026-11-08T12:00:15.000Z'), 'cache', [])
    expect(out.series[0]!.values[1]).toBeCloseTo(209)
    expect(out.series[1]!.values[1]).toBeCloseTo(21)
  })
})
