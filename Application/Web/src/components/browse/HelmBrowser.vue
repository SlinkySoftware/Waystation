<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Helm browse: charts → chart page (install snippet, versions with app version, digest and cached flag). -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="chart = null" /></template>
    <template v-if="!chart" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.helm.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="chart">
      <div v-if="versions.data.value" class="chart">
        <div class="chart__head">
          <div class="col">
            <div class="chart__desc">{{ versions.data.value.chart.description }}</div>
            <div class="chart__meta">
              <span>{{ t('browse.helm.latest') }} <strong class="mono">{{ versions.data.value.chart.latest_version }}</strong></span>
              <span v-if="versions.data.value.chart.app_version">{{ t('browse.helm.appVersion') }} <span class="mono">{{ versions.data.value.chart.app_version }}</span></span>
              <span>{{ t('browse.helm.versionCount', { count: versions.data.value.chart.version_count }) }}</span>
            </div>
          </div>
          <div class="chart__install">
            <div class="section-label">{{ t('browse.helm.install') }}</div>
            <CopyField :value="`helm install my-release ${repo.name}/${chart} --version ${versions.data.value.chart.latest_version}`" />
          </div>
        </div>
        <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" row-key="version" :pagination="{ rowsPerPage: 0 }" hide-pagination>
          <template #body-cell-version="{ row }">
            <q-td>
              <span class="mono ver">{{ row.version }}</span>
              <span v-if="row.deprecated" class="ws-badge tone-degraded dep">{{ t('browse.helm.deprecated') }}</span>
            </q-td>
          </template>
          <template #body-cell-created="{ row }"><q-td><TimeCell :value="row.created_at" /></q-td></template>
          <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td></template>
          <template #body-cell-digest="{ row }">
            <q-td class="mono digest">{{ row.digest ? row.digest.slice(0, 12) : '' }}<q-tooltip v-if="row.digest">sha256:{{ row.digest }}</q-tooltip></q-td>
          </template>
          <template #body-cell-cached="{ row }">
            <q-td><CacheBadge v-if="row.cached" state="HIT" /><span v-else class="text-faint">{{ t('browse.notCached') }}</span></q-td>
          </template>
        </q-table>
      </div>
      <div v-else-if="versions.error.value" class="q-pa-md text-negative">{{ errorMessage(versions.error.value) }}</div>
      <q-skeleton v-else height="300px" class="q-ma-md" />
    </template>

    <q-table
      v-else
      class="ws-table"
      flat
      :rows="charts.data.value?.items ?? []"
      :columns="chartColumns"
      row-key="name"
      :loading="charts.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: HelmChart) => (chart = row.name)"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <div class="mono chart-name">
            {{ row.name }}<span v-if="row.deprecated" class="ws-badge tone-degraded dep">{{ t('browse.helm.deprecated') }}</span>
          </div>
          <div class="chart-desc">{{ row.description }}</div>
        </q-td>
      </template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { HelmChart, HelmVersion, Repository } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const chart = useQueryParam('chart')
const search = ref<string | null>(null)

const charts = useAsync(
  () => call(api.GET('/repositories/{name}/helm/charts', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const versions = useAsync(
  async () => (chart.value ? call(api.GET('/repositories/{name}/helm/versions', { params: { path: { name: props.repo.name }, query: { chart: chart.value, limit: 100 } } })) : null),
  [chart],
)

const crumbs = computed(() => [{ label: t('browse.helm.charts') }, ...(chart.value ? [{ label: chart.value, mono: true }] : [])])

const chartColumns = computed<QTableColumn<HelmChart>[]>(() => [
  { name: 'name', label: t('browse.helm.chart'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.helm.latest'), field: 'latest_version', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'app', label: t('browse.helm.appVersion'), field: (r) => r.app_version ?? '', align: 'left', classes: 'mono gt-sm', headerClasses: 'gt-sm' },
  { name: 'versions', label: t('browse.helm.versions'), field: 'version_count', align: 'right', sortable: true },
])

const columns = computed<QTableColumn<HelmVersion>[]>(() => [
  { name: 'version', label: t('browse.helm.version'), field: 'version', align: 'left' },
  { name: 'app', label: t('browse.helm.appVersion'), field: (r) => r.app_version ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'created', label: t('browse.helm.created'), field: (r) => r.created_at ?? '', align: 'left' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size_bytes ?? 0, align: 'right' },
  { name: 'digest', label: t('browse.helm.digest'), field: (r) => r.digest ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'cached', label: t('browse.cache'), field: 'cached', align: 'left' },
])
</script>

<style scoped>
.browse-search {
  width: 260px;
}

.chart-name {
  font-weight: 600;
}

.chart-desc {
  max-width: 520px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chart__head {
  display: flex;
  flex-wrap: wrap;
  gap: 16px 24px;
  padding: 16px;
  border-top: 1px solid var(--border);
}

.chart__desc {
  font-size: var(--fs-14);
}

.chart__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin-top: 6px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.chart__install {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 280px;
}

.ver {
  font-weight: 600;
}

.digest {
  color: var(--text-secondary);
}

.dep {
  margin-left: 8px;
  height: 18px;
  font-size: 11px;
}
</style>
