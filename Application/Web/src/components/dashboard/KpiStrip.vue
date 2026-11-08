<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- The dashboard KPI row: range totals from /stats/summary, live values from the 1 Hz stats ticks. -->
<template>
  <div class="kpis">
    <KpiTile
      :label="t('dashboard.kpi.requests')"
      :value="formatPerSecond(tick?.requests_per_s ?? summary?.requests_per_s)"
      unit="req/s"
      :sub="t('dashboard.kpi.requestsSub', { count: formatCount(summary?.requests ?? 0), range: rangeLabel })"
      :spark="liveSpark('requests_per_s') ?? summary?.sparks.requests_per_s"
      :loading="!summary"
      live
    />
    <KpiTile
      :label="t('dashboard.kpi.hitRatio')"
      :value="formatPercent(summary?.hit_ratio)"
      :sub="tick?.hit_ratio_1m != null ? t('dashboard.kpi.hitRatioSub', { value: formatPercent(tick.hit_ratio_1m) }) : rangeLabel"
      :spark="summary?.sparks.hit_ratio"
      :spark-min="null"
      color="var(--chart-hit)"
      :loading="!summary"
    />
    <KpiTile
      :label="t('dashboard.kpi.served')"
      :value="formatBytes(served)"
      :sub="t('dashboard.kpi.servedSub', { rate: formatRate(tick?.bytes_out_per_s ?? 0) })"
      :spark="summary?.sparks.bytes_served"
      :loading="!summary"
    />
    <KpiTile
      :label="t('dashboard.kpi.saved')"
      :value="formatBytes(saved)"
      :sub="t('dashboard.kpi.savedSub', { upstream: formatBytes(upstream) })"
      :spark="summary?.sparks.bytes_saved"
      color="var(--chart-hit)"
      :loading="!summary"
    />
    <KpiTile
      :label="t('dashboard.kpi.storage')"
      :value="formatBytes(storageUsed)"
      :sub="t('dashboard.kpi.storageSub', { free: formatBytes(summary?.storage.free_bytes ?? 0), total: formatBytes(summary?.storage.total_bytes ?? 0) })"
      :meter="summary ? storageUsed / summary.storage.total_bytes : 0"
      :meter-label="t('dashboard.kpi.storage')"
      :loading="!summary"
    />
    <KpiTile
      :label="t('dashboard.kpi.artifacts')"
      :value="formatCount(tick?.artifacts_cached ?? summary?.artifacts_cached)"
      :sub="t('dashboard.kpi.artifactsSub')"
      :spark="summary?.sparks.artifacts"
      :spark-min="null"
      color="var(--chart-3)"
      :loading="!summary"
    />
    <KpiTile
      :label="t('dashboard.kpi.active')"
      :value="String(live.activeCount)"
      :sub="t('dashboard.kpi.activeSub', { rate: formatRate(tick?.bytes_upstream_per_s ?? 0) })"
      :spark="liveSpark('active_transfers') ?? summary?.sparks.active_transfers"
      color="var(--chart-2)"
      :loading="!summary"
      live
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import type { StatsRange, StatsSummary, StatsTickEvent } from '@/api/types'
import { t } from '@/i18n'
import { useLiveStore } from '@/stores/live'
import { formatBytes, formatCount, formatPercent, formatPerSecond, formatRate } from '@/utils/format'

import KpiTile from './KpiTile.vue'

const props = defineProps<{ summary: StatsSummary | null; range: StatsRange }>()

const live = useLiveStore()
const tick = computed(() => live.lastTick)
const rangeLabel = computed(() => t(`ranges.${props.range}`))

// Bytes since the summary was fetched, accumulated from ticks, so range totals keep moving.
const extraServed = ref(0)
const extraSaved = ref(0)
const extraUpstream = ref(0)
watch(
  () => props.summary,
  () => {
    extraServed.value = 0
    extraSaved.value = 0
    extraUpstream.value = 0
  },
)
watch(tick, (next) => {
  if (!next || !props.summary) return
  extraServed.value += next.bytes_out_per_s
  extraSaved.value += next.bytes_saved_per_s ?? 0
  extraUpstream.value += next.bytes_upstream_per_s
})

const served = computed(() => (props.summary?.bytes_served ?? 0) + extraServed.value)
const saved = computed(() => (props.summary?.bytes_saved ?? 0) + extraSaved.value)
const upstream = computed(() => (props.summary?.bytes_upstream ?? 0) + extraUpstream.value)
const storageUsed = computed(() => tick.value?.storage_used_bytes ?? props.summary?.storage.used_bytes ?? 0)

/** For the live KPIs over short ranges, the last minute of ticks is the better sparkline. */
function liveSpark(key: 'requests_per_s' | 'active_transfers'): number[] | undefined {
  if (props.range !== '15m' && props.range !== '1h') return undefined
  const ticks: StatsTickEvent[] = live.ticks
  if (ticks.length < 12) return undefined
  return ticks.slice(-60).map((x) => x[key])
}
</script>

<style scoped>
.kpis {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 12px;
}

@media (max-width: 1599px) {
  .kpis {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
}

@media (max-width: 1023px) {
  .kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 599px) {
  .kpis {
    gap: 8px;
  }

  .kpis :deep(.kpi) {
    min-height: 112px;
    padding: 12px;
  }

  .kpis :deep(.kpi__value) {
    font-size: 22px;
  }
}
</style>
