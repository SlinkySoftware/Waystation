<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Storage growth (UI_DESIGN §4.7) from the hourly storage snapshots: bytes on disk stacked with the bytes
     de-duplication saves (together: the stored bytes), over 7 days to a year. -->
<template>
  <PageCard :title="t('storage.growth.title')" :caption="caption">
    <template #actions>
      <q-btn-toggle v-model="range" no-caps dense unelevated toggle-color="primary" class="seg" :options="rangeOptions" :aria-label="t('storage.growth.range')" />
    </template>
    <div v-if="error && !data" class="text-negative">{{ t('storage.growth.loadFailed') }}</div>
    <q-skeleton v-else-if="!data" height="240px" />
    <EmptyState v-else-if="growth.empty" compact :icon="iconTrend" :title="t('storage.growth.pendingTitle')" :text="t('storage.growth.pendingText')" />
    <StackedAreaChart
      v-else
      :timestamps="data.timestamps"
      :series="series"
      :step-seconds="data.step_s"
      :label="t('storage.growth.aria', { range: t(`ranges.${range}`) })"
      :format="formatBytes"
      :height="240"
      :empty-text="t('storage.growth.pendingTitle')"
    />
  </PageCard>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { api, call } from '@/api/client'
import type { HistoryRange } from '@/api/types'
import StackedAreaChart from '@/components/charts/StackedAreaChart.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { formatBytes } from '@/utils/format'
import { iconTrend } from '@/utils/icons'
import { growthDelta, growthSeries } from '@/utils/topStats'

const RANGES: HistoryRange[] = ['7d', '30d', '90d', '1y']
const range = ref<HistoryRange>('30d')
const rangeOptions = computed(() => RANGES.map((r) => ({ label: t(`ranges.short.${r}`), value: r })))

const { data, error } = useAsync(() => call(api.GET('/stats/storage/history', { params: { query: { range: range.value } } })), [range])

const growth = computed(() => growthSeries(data.value ?? { used_bytes: [], stored_bytes: [] }))
const series = computed(() => [
  { key: 'used', label: t('storage.growth.used'), color: 'var(--chart-3)', values: growth.value.used },
  { key: 'saved', label: t('storage.growth.saved'), color: 'var(--chart-hit)', values: growth.value.saved },
])
const caption = computed(() => {
  const delta = data.value ? growthDelta(data.value.used_bytes) : null
  if (delta === null) return t('storage.growth.caption')
  const sign = delta > 0 ? '+' : delta < 0 ? '−' : ''
  return t('storage.growth.captionDelta', { delta: `${sign}${formatBytes(Math.abs(delta))}`, range: t(`ranges.${range.value}`) })
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
}
</style>
