// SPDX-License-Identifier: GPL-3.0-or-later
// Synthetic, deterministic traffic history (same bucket = same value on every request), joined to the live world
// for the most recent 15 minutes.
import type { StatsRange, StatsSummary, TimeSeries } from '@/api/types'

import { hash, MiB, rng } from './rng'
import type { MockWorld } from './world'

export const RANGES: Record<StatsRange, { seconds: number; step: number }> = {
  '15m': { seconds: 900, step: 10 },
  '1h': { seconds: 3600, step: 60 },
  '24h': { seconds: 86400, step: 600 },
  '7d': { seconds: 7 * 86400, step: 3600 },
  '30d': { seconds: 30 * 86400, step: 6 * 3600 },
}

const FORMAT_SHARE: [string, string, number][] = [
  ['docker', 'Docker', 0.55],
  ['npm', 'npm', 0.11],
  ['apt', 'apt', 0.18],
  ['yum', 'yum', 0.12],
  ['raw', 'Raw', 0.04],
]

/** Average bytes/s served at time `t` (ms): diurnal + weekday shape, seeded noise and bursts. */
function rateAt(t: number, step: number, salt = ''): { hit: number; upstream: number; requests: number } {
  const d = new Date(t)
  const hour = d.getHours() + d.getMinutes() / 60
  const weekend = d.getDay() === 0 || d.getDay() === 6
  const diurnal = 0.18 + Math.max(0, Math.sin(((hour - 7) / 15) * Math.PI)) * (weekend ? 0.45 : 1)
  const next = rng(hash(`${salt}${Math.floor(t / 1000 / step)}:${step}`))
  // Shorter buckets are spikier: a single docker pull dominates a 10 s bucket but not a day.
  const spike = step <= 60 ? (next() < 0.22 ? 2 + next() * 6 : 0.4 + next() * 0.8) : step <= 600 ? (next() < 0.1 ? 1.3 + next() * 0.9 : 0.8 + next() * 0.4) : 0.85 + next() * 0.3
  const hit = 9.2 * MiB * diurnal * spike
  const upstreamShare = 0.04 + next() * (spike > 2 ? 0.35 : 0.1)
  return { hit, upstream: hit * upstreamShare, requests: 32 * diurnal * (0.6 + spike * 0.4) }
}

function buckets(range: StatsRange): number[] {
  const { seconds, step } = RANGES[range]
  const end = Math.floor(Date.now() / 1000 / step) * step * 1000
  const n = Math.round(seconds / step)
  return Array.from({ length: n }, (_, i) => end - (n - 1 - i) * step * 1000)
}

export function trafficSeries(world: MockWorld, range: StatsRange, metric: 'bytes' | 'requests', split: 'cache' | 'format', repo?: string | null): TimeSeries {
  const { step } = RANGES[range]
  const ts = buckets(range)
  const scale = repo ? repoShare(world, repo) : 1
  const live = new Map(world.history.map((h) => [h.ts, h]))
  const rows = ts.map((t) => {
    const r = rateAt(t, step, repo ?? '')
    const l = range === '15m' && !repo ? live.get(t) : undefined
    if (l) return { hit: l.hit, upstream: l.upstream, requests: r.requests * step }
    return { hit: r.hit * step * scale, upstream: r.upstream * step * scale, requests: r.requests * step * scale }
  })
  const value = (row: { hit: number; upstream: number; requests: number }, part: 'hit' | 'upstream') =>
    metric === 'bytes' ? Math.round(row[part]) : Math.round(row.requests * (part === 'hit' ? 0.92 : 0.08))
  const series =
    split === 'cache'
      ? [
          { key: 'hit', label: 'Served from cache', values: rows.map((r) => value(r, 'hit')) },
          { key: 'upstream', label: 'Fetched from upstream', values: rows.map((r) => value(r, 'upstream')) },
        ]
      : FORMAT_SHARE.map(([key, label, share], k) => ({
          key,
          label,
          values: rows.map((r, i) => {
            const wobble = 0.7 + rng(hash(`${key}:${ts[i]}`))() * 0.6
            const total = metric === 'bytes' ? r.hit + r.upstream : r.requests
            return Math.round(total * share * wobble * (k === 0 ? 1 : 1))
          }),
        }))
  return { range, step_s: step, unit: metric, timestamps: ts.map((t) => new Date(t).toISOString()), series }
}

function repoShare(world: MockWorld, repo: string): number {
  const total = world.repos.reduce((a, r) => a + r.stats.bytes_served_24h, 0) || 1
  const r = world.repos.find((x) => x.name === repo)
  return r ? r.stats.bytes_served_24h / total : 0
}

/** Down-sample a series to at most `points` values (sums per group). */
function downsample(values: number[], points: number): number[] {
  if (values.length <= points) return values
  const size = values.length / points
  return Array.from({ length: points }, (_, i) => values.slice(Math.floor(i * size), Math.floor((i + 1) * size)).reduce((a, b) => a + b, 0))
}

export function statsSummary(world: MockWorld, range: StatsRange): StatsSummary {
  const bytes = trafficSeries(world, range, 'bytes', 'cache')
  const reqs = trafficSeries(world, range, 'requests', 'cache')
  const hit = bytes.series[0]!.values
  const up = bytes.series[1]!.values
  const rq = reqs.series[0]!.values.map((v, i) => v + reqs.series[1]!.values[i]!)
  const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0)
  const points = 30
  const sparkHit = downsample(hit, points)
  const sparkUp = downsample(up, points)
  const sparkReq = downsample(rq, points)
  const step = (RANGES[range].seconds / sparkReq.length) | 0
  const used = world.storageUsed
  const growth = Array.from({ length: points }, (_, i) => used - (points - 1 - i) * (RANGES[range].seconds / 86400) * 0.9 * 1024 * MiB * (0.8 + rng(hash(`g${i}`))() * 0.4) / points)
  const live = world.transfers()
  return {
    range,
    step_s: step,
    generated_at: new Date().toISOString(),
    requests_per_s: Math.round((sum(rq.slice(-6)) / (6 * RANGES[range].step)) * 10) / 10,
    requests: Math.round(sum(rq)),
    hit_ratio: 0.93,
    bytes_served: Math.round(sum(hit) + sum(up)),
    bytes_saved: Math.round(sum(hit)),
    bytes_upstream: Math.round(sum(up)),
    storage: { used_bytes: used, free_bytes: world.storageTotal - used - 312 * 1024 * MiB, total_bytes: world.storageTotal },
    artifacts_cached: world.artifacts,
    active_transfers: live.length,
    sparks: {
      requests_per_s: sparkReq.map((v) => Math.round((v / step) * 10) / 10),
      hit_ratio: sparkHit.map((h, i) => {
        const total = h + sparkUp[i]!
        return total ? Math.round((h / total) * 1000) / 1000 : null
      }),
      bytes_served: sparkHit.map((h, i) => h + sparkUp[i]!),
      bytes_saved: sparkHit,
      storage_used: growth.map(Math.round),
      artifacts: growth.map((g, i) => Math.round(world.artifacts - (points - 1 - i) * 37)),
      active_transfers: Array.from({ length: points }, (_, i) => Math.round(2 + rng(hash(`at${i}${range}`))() * 14)),
    },
  }
}
