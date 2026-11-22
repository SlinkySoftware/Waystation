<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Composer browse: packages → package page (composer require snippet, versions newest first as the server orders
     them, with stability badges, the source reference, cache state, size, publish date and download link; hosted: who
     uploaded it). Groups list their members' packages. -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="pkg = null" /></template>
    <template v-if="!pkg" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.composer.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="pkg">
      <div v-if="versions.data.value" class="pkg">
        <div class="pkg__head">
          <div class="col">
            <div class="pkg__name mono">{{ versions.data.value.package.name }}</div>
            <div v-if="versions.data.value.package.description" class="pkg__desc">{{ versions.data.value.package.description }}</div>
            <div class="pkg__meta">
              <span v-if="versions.data.value.package.latest_version">{{ t('browse.composer.latest') }} <strong class="mono">{{ versions.data.value.package.latest_version }}</strong></span>
              <span>{{ t('browse.composer.versionCount', { count: versions.data.value.package.version_count }) }}</span>
              <span v-if="versions.data.value.package.type" class="mono">{{ versions.data.value.package.type }}</span>
            </div>
          </div>
          <div v-if="versions.data.value.package.latest_version" class="pkg__install">
            <div class="section-label">{{ t('browse.composer.install') }}</div>
            <CopyField :value="`composer require ${versions.data.value.package.name}:${versions.data.value.package.latest_version}`" />
          </div>
        </div>

        <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" row-key="version" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('browse.empty')">
          <template #body-cell-version="{ row }">
            <q-td>
              <span class="mono ver">{{ row.version }}</span>
              <span v-if="row.stability !== 'stable'" class="ws-badge badge" :class="row.stability === 'dev' ? 'tone-degraded' : 'tone-offline'">{{ stabilityLabel(row.stability) }}</span>
              <div v-if="row.uploaded_by" class="by">{{ t('browse.composer.uploadedBy', { user: row.uploaded_by }) }}</div>
            </q-td>
          </template>
          <template #body-cell-reference="{ row }">
            <q-td>
              <span v-if="row.reference" class="mono ref">
                {{ row.reference.slice(0, 7) }}
                <q-tooltip>{{ row.reference }}</q-tooltip>
              </span>
            </q-td>
          </template>
          <template #body-cell-published="{ row }"><q-td><TimeCell :value="row.published_at ?? null" placeholder="" /></q-td></template>
          <template #body-cell-size="{ row }">
            <q-td class="text-right"><SizeCell v-if="row.size_bytes !== null && row.size_bytes !== undefined" :bytes="row.size_bytes" /></q-td>
          </template>
          <template #body-cell-cached="{ row }">
            <q-td><CacheBadge v-if="row.cached" state="HIT" /><span v-else class="text-faint">{{ t('browse.notCached') }}</span></q-td>
          </template>
          <template #body-cell-actions="{ row }">
            <q-td auto-width class="actions">
              <template v-if="row.download_url">
                <q-btn flat dense round size="sm" :icon="iconCopy" :aria-label="t('browse.copyUrl')" @click="copy(row.download_url, t('browse.url'))">
                  <q-tooltip>{{ t('browse.copyUrl') }}</q-tooltip>
                </q-btn>
                <q-btn flat dense round size="sm" type="a" :href="row.download_url" :icon="iconDownload" :aria-label="t('browse.composer.download')">
                  <q-tooltip>{{ t('browse.composer.download') }}</q-tooltip>
                </q-btn>
              </template>
            </q-td>
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
      :rows="packages.data.value?.items ?? []"
      :columns="packageColumns"
      row-key="name"
      :loading="packages.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: ComposerPackage) => (pkg = row.name)"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <div class="mono pkg-name">{{ row.name }}</div>
          <div class="pkg-desc">
            <span v-if="row.type">{{ row.type }}</span><span v-if="row.type && row.description"> · </span>{{ row.description }}
          </div>
        </q-td>
      </template>
      <template #body-cell-updated="{ row }"><q-td><TimeCell :value="row.updated_at ?? null" placeholder="" /></q-td></template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { ComposerPackage, ComposerVersion, Repository } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy } from '@/composables/useNotify'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { iconCopy, iconDownload, iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const copy = useCopy()
const pkg = useQueryParam('package')
const search = ref<string | null>(null)

const packages = useAsync(
  () => call(api.GET('/repositories/{name}/composer/packages', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const versions = useAsync(
  async () => (pkg.value ? call(api.GET('/repositories/{name}/composer/versions', { params: { path: { name: props.repo.name }, query: { package: pkg.value, limit: 200 } } })) : null),
  [pkg],
)

const crumbs = computed(() => [{ label: t('browse.composer.packages') }, ...(pkg.value ? [{ label: versions.data.value?.package.name ?? pkg.value, mono: true }] : [])])

const stabilityLabel = (stability: ComposerVersion['stability']): string => t(`browse.composer.stability.${stability}`)

const packageColumns = computed<QTableColumn<ComposerPackage>[]>(() => [
  { name: 'name', label: t('browse.composer.package'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.composer.latest'), field: (r) => r.latest_version ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'versions', label: t('browse.composer.versions'), field: 'version_count', align: 'right', sortable: true },
  { name: 'updated', label: t('browse.modified'), field: (r) => r.updated_at ?? '', align: 'left', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
])

const columns = computed<QTableColumn<ComposerVersion>[]>(() => [
  { name: 'version', label: t('browse.composer.version'), field: 'version', align: 'left' },
  { name: 'reference', label: t('browse.composer.reference'), field: (r) => r.reference ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'published', label: t('browse.composer.published'), field: (r) => r.published_at ?? '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size_bytes ?? 0, align: 'right' },
  { name: 'cached', label: t('browse.cache'), field: 'cached', align: 'left' },
  { name: 'actions', label: '', field: () => '', align: 'right' },
])
</script>

<style scoped>
.browse-search {
  width: 260px;
}

.pkg-name {
  font-weight: 600;
}

.pkg-desc {
  max-width: 560px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pkg__head {
  display: flex;
  flex-wrap: wrap;
  gap: 16px 24px;
  padding: 16px;
  border-top: 1px solid var(--border);
}

.pkg__name {
  font-size: var(--fs-16);
  font-weight: 650;
}

.pkg__desc {
  margin-top: 4px;
  font-size: var(--fs-14);
}

.pkg__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin-top: 6px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.pkg__install {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 280px;
}

.ver {
  font-weight: 600;
}

.ref {
  color: var(--text-secondary);
}

.badge {
  margin-left: 8px;
  height: 18px;
  font-size: 11px;
}

.by {
  margin-top: 2px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.actions {
  white-space: nowrap;
}
</style>
