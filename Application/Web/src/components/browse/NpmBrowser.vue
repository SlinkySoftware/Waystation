<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- npm browse: packages → package page (install snippet, dist-tags, versions with cached/deprecated flags). -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="pkg = null" /></template>
    <template v-if="!pkg" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.npm.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="pkg">
      <div v-if="versions.data.value" class="pkg">
        <div class="pkg__head">
          <div class="col">
            <div class="pkg__desc">{{ versions.data.value.package.description }}</div>
            <div class="pkg__meta">
              <span>{{ t('browse.npm.latest') }} <strong class="mono">{{ versions.data.value.package.latest }}</strong></span>
              <span v-if="versions.data.value.package.license">{{ versions.data.value.package.license }}</span>
              <span>{{ t('browse.npm.cachedOf', { cached: versions.data.value.package.cached_versions, total: versions.data.value.package.version_count }) }}</span>
            </div>
            <div class="pkg__tags">
              <span v-for="(v, tag) in versions.data.value.dist_tags ?? {}" :key="tag" class="dist-tag"><span>{{ tag }}</span><span class="mono">{{ v }}</span></span>
            </div>
          </div>
          <div class="pkg__install">
            <div class="section-label">{{ t('browse.npm.install') }}</div>
            <CopyField :value="`npm install ${pkg}@${versions.data.value.package.latest}`" />
          </div>
        </div>
        <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" row-key="version" :pagination="{ rowsPerPage: 0 }" hide-pagination>
          <template #body-cell-version="{ row }">
            <q-td>
              <span class="mono ver">{{ row.version }}</span>
              <span v-for="tag in row.dist_tags ?? []" :key="tag" class="dist-tag dist-tag--small">{{ tag }}</span>
              <span v-if="row.deprecated" class="ws-badge tone-degraded dep">{{ t('browse.npm.deprecated') }}<q-tooltip max-width="360px">{{ row.deprecated }}</q-tooltip></span>
            </q-td>
          </template>
          <template #body-cell-published="{ row }"><q-td><TimeCell :value="row.published_at" /></q-td></template>
          <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td></template>
          <template #body-cell-cached="{ row }">
            <q-td><CacheBadge v-if="row.cached" state="HIT" /><span v-else class="text-faint">{{ t('browse.notCached') }}</span></q-td>
          </template>
          <template #body-cell-last="{ row }"><q-td><TimeCell :value="row.last_downloaded_at" :placeholder="t('repos.never')" /></q-td></template>
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
      :columns="pkgColumns"
      row-key="name"
      :loading="packages.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: NpmPackage) => (pkg = row.name)"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <div class="mono pkg-name">{{ row.name }}</div>
          <div class="pkg-desc">{{ row.description }}</div>
        </q-td>
      </template>
      <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td></template>
      <template #body-cell-last="{ row }"><q-td><TimeCell :value="row.last_downloaded_at" :placeholder="t('repos.never')" /></q-td></template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { NpmPackage, NpmVersion, Repository } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const pkg = useQueryParam('package')
const search = ref<string | null>(null)

const packages = useAsync(
  () => call(api.GET('/repositories/{name}/npm/packages', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const versions = useAsync(
  async () => (pkg.value ? call(api.GET('/repositories/{name}/npm/versions', { params: { path: { name: props.repo.name }, query: { package: pkg.value, limit: 100 } } })) : null),
  [pkg],
)

const crumbs = computed(() => [{ label: t('browse.npm.packages') }, ...(pkg.value ? [{ label: pkg.value, mono: true }] : [])])

const pkgColumns = computed<QTableColumn<NpmPackage>[]>(() => [
  { name: 'name', label: t('browse.npm.package'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.npm.latest'), field: 'latest', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'versions', label: t('browse.npm.cached'), field: (r) => `${r.cached_versions} / ${r.version_count}`, align: 'right', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right', sortable: true },
  { name: 'downloads', label: t('browse.downloads'), field: (r) => r.downloads ?? 0, format: (v: number) => formatCount(v), align: 'right', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'last', label: t('browse.lastDownloaded'), field: (r) => r.last_downloaded_at ?? '', align: 'left', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
])

const columns = computed<QTableColumn<NpmVersion>[]>(() => [
  { name: 'version', label: t('browse.npm.version'), field: 'version', align: 'left' },
  { name: 'published', label: t('browse.npm.published'), field: 'published_at', align: 'left' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size_bytes ?? 0, align: 'right' },
  { name: 'downloads', label: t('browse.downloads'), field: (r) => r.downloads ?? 0, format: (v: number) => formatCount(v), align: 'right', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'cached', label: t('browse.cache'), field: 'cached', align: 'left' },
  { name: 'last', label: t('browse.lastDownloaded'), field: (r) => r.last_downloaded_at ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
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
  max-width: 520px;
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

.pkg__desc {
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

.pkg__tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.pkg__install {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 280px;
}

.dist-tag {
  display: inline-flex;
  gap: 6px;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface-brand);
  font-size: var(--fs-12);
}

.dist-tag span:first-child {
  font-weight: 600;
  color: var(--q-primary);
}

.dist-tag--small {
  margin-left: 8px;
  font-weight: 600;
  color: var(--q-primary);
}

.ver {
  font-weight: 600;
}

.dep {
  margin-left: 8px;
  height: 18px;
  font-size: 11px;
}
</style>
