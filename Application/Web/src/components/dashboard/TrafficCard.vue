<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Traffic over time: stacked area, served-from-cache vs fetched-from-upstream or by format; bytes or requests. -->
<template>
  <PageCard :title="title ?? t('dashboard.traffic.title')" :caption="caption">
    <template #actions>
      <q-btn-toggle v-if="!repository" v-model="split" no-caps dense unelevated toggle-color="primary" class="seg" :options="splitOptions" />
      <q-btn-toggle v-model="metric" no-caps dense unelevated toggle-color="primary" class="seg" :options="metricOptions" />
    </template>
    <div v-if="error && !data" class="text-negative">{{ errorMessage(error) }}</div>
    <q-skeleton v-else-if="!data" :height="`${height}px`" />
    <StackedAreaChart
      v-else
      :timestamps="data.timestamps"
      :series="chartSeries"
      :step-seconds="data.step_s"
      :label="t('dashboard.traffic.aria', { range: t(`ranges.${range}`) })"
      :format="metric === 'bytes' ? formatRate : formatReq"
      :height="height"
      :empty-text="t('dashboard.traffic.empty')"
    />
  </PageCard>
</template>

<script setup lang="ts">
import { computed, ref, toRef } from 'vue'

import { errorMessage } from '@/api/client'
import type { StatsRange } from '@/api/types'
import StackedAreaChart from '@/components/charts/StackedAreaChart.vue'
import PageCard from '@/components/PageCard.vue'
import { type TrafficMetric, type TrafficSplit, useTraffic } from '@/composables/useTraffic'
import { t } from '@/i18n'
import { useLiveStore } from '@/stores/live'
import { formatColor } from '@/utils/domain'
import { formatPerSecond, formatRate } from '@/utils/format'

const props = withDefaults(defineProps<{ range: StatsRange; repository?: string | null; title?: string; height?: number }>(), {
  repository: null,
  title: undefined,
  height: 260,
})

const live = useLiveStore()
const split = ref<TrafficSplit>('cache')
const metric = ref<TrafficMetric>('bytes')
const { data, error } = useTraffic(toRef(props, 'range'), split, metric, { ticks: toRef(live, 'lastTick'), transfers: toRef(live, 'transfers') }, toRef(props, 'repository'))

const splitOptions = computed(() => [
  { label: t('dashboard.traffic.byCache'), value: 'cache' },
  { label: t('dashboard.traffic.byFormat'), value: 'format' },
])
const metricOptions = computed(() => [
  { label: t('dashboard.traffic.bytes'), value: 'bytes' },
  { label: t('dashboard.traffic.requests'), value: 'requests' },
])
const caption = computed(() => (metric.value === 'bytes' ? t('dashboard.traffic.captionBytes') : t('dashboard.traffic.captionRequests')))

const formatReq = (v: number) => `${formatPerSecond(v)} req/s`

/** Bucket totals → average per second, so every range reads in the same unit. */
const chartSeries = computed(() => {
  const d = data.value
  if (!d) return []
  return d.series.map((s) => ({
    key: s.key,
    label: s.key === 'hit' ? t('dashboard.traffic.hit') : s.key === 'upstream' ? t('dashboard.traffic.upstream') : s.label,
    color: s.key === 'hit' ? 'var(--chart-hit)' : s.key === 'upstream' ? 'var(--chart-upstream)' : formatColor(s.key),
    values: s.values.map((v) => v / d.step_s),
  }))
})
</script>

<style scoped>
.seg {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.seg :deep(.q-btn) {
  padding: 0 10px;
  min-height: 28px;
  font-size: var(--fs-12);
}
</style>
