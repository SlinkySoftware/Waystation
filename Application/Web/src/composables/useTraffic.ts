// SPDX-License-Identifier: GPL-3.0-or-later
// Traffic time series for a range, kept current from live stats ticks: each tick adds its bytes (or requests) to
// the newest bucket and rolls a new bucket when the step boundary passes, so the chart moves without polling.
import { type Ref, ref, shallowRef, watch } from 'vue'

import { api, call } from '@/api/client'
import type { StatsRange, StatsTickEvent, TimeSeries } from '@/api/types'
import type { LiveTransfer } from '@/stores/live'

export type TrafficSplit = 'cache' | 'format'
export type TrafficMetric = 'bytes' | 'requests'

/** Add one tick's worth of traffic to a series, rolling buckets as time passes. Pure, for testing. */
export function applyTick(series: TimeSeries, tick: StatsTickEvent, split: TrafficSplit, transfers: LiveTransfer[], seconds = 1): TimeSeries {
  const stepMs = series.step_s * 1000
  const now = Date.parse(tick.ts)
  const timestamps = [...series.timestamps]
  const values = series.series.map((s) => [...s.values])
  let last = Date.parse(timestamps[timestamps.length - 1] ?? tick.ts)
  while (now >= last + stepMs) {
    last += stepMs
    timestamps.push(new Date(last).toISOString())
    timestamps.shift()
    for (const v of values) {
      v.push(0)
      v.shift()
    }
  }
  const add = (key: string, amount: number) => {
    const k = series.series.findIndex((s) => s.key === key)
    if (k >= 0 && amount > 0) values[k]![values[k]!.length - 1]! += amount
  }
  if (series.unit === 'requests') {
    const reqs = tick.requests_per_s * seconds
    if (split === 'cache') {
      const ratio = tick.hit_ratio_1m ?? 0.9
      add('hit', reqs * ratio)
      add('upstream', reqs * (1 - ratio))
    } else {
      const active = transfers.filter((x) => x.state === 'active')
      for (const s of series.series) add(s.key, (reqs * active.filter((x) => x.format === s.key).length) / Math.max(1, active.length))
    }
  } else if (split === 'cache') {
    add('hit', (tick.bytes_saved_per_s ?? tick.bytes_out_per_s - tick.bytes_upstream_per_s) * seconds)
    add('upstream', tick.bytes_upstream_per_s * seconds)
  } else {
    for (const x of transfers) if (x.state === 'active') add(x.format, x.rate_bps * seconds)
  }
  return { ...series, timestamps, series: series.series.map((s, k) => ({ ...s, values: values[k]! })) }
}

export function useTraffic(
  range: Ref<StatsRange>,
  split: Ref<TrafficSplit>,
  metric: Ref<TrafficMetric>,
  live: { ticks: Ref<StatsTickEvent | null>; transfers: Ref<LiveTransfer[]> },
  repo?: Ref<string | null>,
) {
  const data = shallowRef<TimeSeries | null>(null)
  const loading = ref(false)
  const error = ref<unknown>(null)
  let generation = 0

  async function load(): Promise<void> {
    const mine = ++generation
    loading.value = true
    try {
      const query = { range: range.value, metric: metric.value }
      const value = repo?.value
        ? await call(api.GET('/repositories/{name}/stats/traffic', { params: { path: { name: repo.value }, query } }))
        : await call(api.GET('/stats/traffic', { params: { query: { ...query, split: split.value } } }))
      if (mine === generation) {
        data.value = value
        error.value = null
      }
    } catch (cause) {
      if (mine === generation) error.value = cause
    } finally {
      if (mine === generation) loading.value = false
    }
  }

  watch([range, split, metric, ...(repo ? [repo] : [])], () => void load(), { immediate: true })
  watch(live.ticks, (tick) => {
    // Per-repository charts are not fed by the global ticks (they would over-count).
    if (!tick || !data.value || loading.value || repo?.value) return
    data.value = applyTick(data.value, tick, split.value, live.transfers.value)
  })

  return { data, loading, error, reload: load }
}
