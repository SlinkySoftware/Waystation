<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Storage (UI_DESIGN §4.7): totals, the data volume, de-duplication savings, bytes by format and repository, blob GC. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.storage')" :caption="t('storage.caption')">
      <template #actions>
        <q-btn outline no-caps :icon="iconRefresh" :label="t('storage.refresh')" :loading="loading && !!data" @click="reload" />
      </template>
    </PageHeader>

    <q-card v-if="error && !data" class="q-pa-lg">
      <EmptyState :icon="iconError" :title="t('storage.loadFailed')" :text="errorMessage(error)">
        <q-btn unelevated no-caps color="primary" :label="t('common.retry')" @click="reload" />
      </EmptyState>
    </q-card>

    <template v-else>
      <div class="st-kpis">
        <KpiTile
          :label="t('storage.kpi.used')"
          :value="formatBytes(used)"
          :sub="t('storage.kpi.usedSub', { share: formatPercent(volume.total ? used / volume.total : null) })"
          :meter="volume.total ? used / volume.total : 0"
          :meter-label="t('storage.kpi.used')"
          :loading="!data"
          :live="liveUsed !== null"
        />
        <KpiTile :label="t('storage.kpi.free')" :value="formatBytes(volume.free)" :sub="t('storage.kpi.freeSub', { total: formatBytes(volume.total) })" :loading="!data" />
        <KpiTile
          :label="t('storage.kpi.dedup')"
          :value="formatBytes(dedup.saved)"
          :sub="t('storage.kpi.dedupSub', { share: formatPercent(dedup.share), logical: formatBytes(dedup.logical) })"
          :meter="dedup.share"
          :meter-label="t('storage.kpi.dedup')"
          :loading="!data"
        />
        <KpiTile :label="t('storage.kpi.blobs')" :value="formatCount(data?.blob_count)" :sub="t('storage.kpi.blobsSub', { artifacts: formatCount(artifacts) })" :loading="!data" />
      </div>

      <PageCard :title="t('storage.volume.title')" :caption="t('storage.volume.caption')" class="q-mb-md">
        <q-skeleton v-if="!data" height="48px" />
        <VolumeBar v-else :volume="volume" />
      </PageCard>

      <div class="st-grid">
        <PageCard :title="t('storage.byFormat.title')" :caption="t('storage.byFormat.caption')">
          <q-skeleton v-if="!data" height="160px" />
          <EmptyState v-else-if="!formatItems.length" compact :icon="iconStorage" :title="t('storage.table.none')" />
          <DonutChart v-else :items="formatItems" :label="t('storage.byFormat.title')" :format="formatBytes" :caption="t('storage.byFormat.total')">
            <template #label="{ item }">
              <FormatIcon :mark="formatMark(item.key as Format)" :size="14" decorative />
              {{ item.label }}
            </template>
          </DonutChart>
        </PageCard>
        <PageCard :title="t('storage.largest.title')" :caption="t('storage.largest.caption', { count: TOP })">
          <q-skeleton v-if="!data" height="160px" />
          <EmptyState v-else-if="!top.length" compact :icon="iconStorage" :title="t('storage.table.none')" />
          <BarList v-else :items="topItems" :label="t('storage.largest.aria')" :format="formatBytes" :total="referenced">
            <template #label="{ item }">
              <FormatIcon :mark="repos.markFor(item.key, rowFormat(item.key))" :size="14" decorative />
              <router-link :to="{ name: 'repository', params: { name: item.key } }" class="text-link">{{ item.label }}</router-link>
            </template>
          </BarList>
        </PageCard>
      </div>

      <StorageTable :rows="rows" :loading="!data" class="q-mb-md" />

      <GcCard v-if="isAdmin" :can-run="isAdmin" class="q-mb-md" />

      <PageCard :title="t('storage.growth.title')" :caption="t('storage.growth.caption')">
        <EmptyState compact :icon="iconTrend" :title="t('storage.growth.pendingTitle')" :text="t('storage.growth.pendingText')" />
      </PageCard>
    </template>
  </q-page>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { Format } from '@/api/types'
import BarList from '@/components/charts/BarList.vue'
import GcCard from '@/components/cleanup/GcCard.vue'
import DonutChart from '@/components/charts/DonutChart.vue'
import KpiTile from '@/components/dashboard/KpiTile.vue'
import EmptyState from '@/components/EmptyState.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import PageCard from '@/components/PageCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import StorageTable from '@/components/storage/StorageTable.vue'
import VolumeBar from '@/components/storage/VolumeBar.vue'
import { useAsync } from '@/composables/useAsync'
import { useLiveTopic } from '@/composables/useLiveTopic'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'
import { useLiveStore } from '@/stores/live'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatColor, formatLabel, formatMark } from '@/utils/domain'
import { formatBytes, formatCount, formatPercent } from '@/utils/format'
import { iconError, iconRefresh, iconStorage, iconTrend } from '@/utils/icons'
import { dedupFigures, largest, repoRows, volumeBreakdown } from '@/utils/storageStats'

const isAdmin = computed(() => useAuthStore().can('admin'))
const TOP = 8

const live = useLiveStore()
const repos = useRepositoriesStore()
useLiveTopic(['stats'])
onMounted(() => void repos.load().catch(() => undefined))

const { data, error, loading, reload } = useAsync(() => call(api.GET('/stats/storage')))

// The 1 Hz stats tick carries the current stored bytes: keep the headline live between refreshes.
const liveUsed = computed(() => (data.value ? (live.lastTick?.storage_used_bytes ?? null) : null))
const used = computed(() => liveUsed.value ?? data.value?.used_bytes ?? 0)
const volume = computed(() => volumeBreakdown({ used_bytes: used.value, free_bytes: data.value?.free_bytes ?? 0, total_bytes: data.value?.total_bytes ?? 0 }))
const dedup = computed(() => dedupFigures({ used_bytes: data.value?.used_bytes ?? 0, dedup_saved_bytes: data.value?.dedup_saved_bytes ?? 0 }))
const rows = computed(() => (data.value ? repoRows(data.value) : []))
const artifacts = computed(() => rows.value.reduce((a, r) => a + r.artifacts, 0))
const referenced = computed(() => rows.value.reduce((a, r) => a + Math.max(0, r.bytes), 0))
const top = computed(() => largest(rows.value, TOP))
const topItems = computed(() => top.value.map((r) => ({ key: r.repository, label: r.repository, value: r.bytes, color: formatColor(r.format) })))
const rowFormat = (name: string): Format => rows.value.find((r) => r.repository === name)?.format ?? 'raw'
const formatItems = computed(() =>
  (data.value?.by_format ?? []).filter((f) => f.bytes > 0).map((f) => ({ key: f.format, label: formatLabel(f.format), value: f.bytes, color: formatColor(f.format) })),
)
</script>

<style scoped>
.st-kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

.st-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

@media (max-width: 1023px) {
  .st-kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .st-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 599px) {
  .st-kpis {
    grid-template-columns: 1fr;
    gap: 12px;
  }
}
</style>
