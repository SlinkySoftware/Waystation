<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Ansible Galaxy browse: namespaces → collections → collection page (install snippet, versions newest first by
     SemVer: requires_ansible, published, size, sha256, dependencies, publisher, cached flag). Groups show their
     members' collections (a version several members hold is shown once, from the first member). -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="go" /></template>
    <template v-if="!collection" #actions>
      <q-input
        v-model="search"
        dense
        outlined
        clearable
        debounce="200"
        :placeholder="namespace ? t('browse.galaxy.searchCollections') : t('browse.galaxy.search')"
        class="browse-search"
      >
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="namespace && collection">
      <div v-if="versions.data.value" class="coll">
        <div class="coll__head">
          <div class="col">
            <div class="coll__name mono">{{ namespace }}.{{ collection }}</div>
            <div v-if="versions.data.value.collection.description" class="coll__desc">{{ versions.data.value.collection.description }}</div>
            <div class="coll__meta">
              <span>{{ t('browse.galaxy.latest') }} <strong class="mono">{{ versions.data.value.collection.latest_version }}</strong></span>
              <span v-if="versions.data.value.collection.requires_ansible">
                {{ t('browse.galaxy.requiresAnsible') }} <span class="mono">{{ versions.data.value.collection.requires_ansible }}</span>
              </span>
              <span>{{ t('browse.galaxy.versionCount', { count: versions.data.value.collection.version_count }) }}</span>
            </div>
          </div>
          <div class="coll__install">
            <div class="section-label">{{ t('browse.galaxy.install') }}</div>
            <CopyField :value="`ansible-galaxy collection install ${namespace}.${collection}:==${versions.data.value.collection.latest_version} -s ${repoUrl}`" />
          </div>
        </div>
        <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" row-key="version" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('browse.empty')">
          <template #body-cell-version="{ row }">
            <q-td>
              <span class="mono ver">{{ row.version }}</span>
              <span v-if="row.prerelease" class="ws-badge tone-offline badge">{{ t('browse.galaxy.prerelease') }}</span>
            </q-td>
          </template>
          <template #body-cell-created="{ row }"><q-td><TimeCell :value="row.created_at ?? null" placeholder="" /></q-td></template>
          <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes ?? null" /></q-td></template>
          <template #body-cell-sha="{ row }">
            <q-td class="mono digest">{{ row.sha256 ? row.sha256.slice(0, 12) : '' }}<q-tooltip v-if="row.sha256">sha256:{{ row.sha256 }}</q-tooltip></q-td>
          </template>
          <template #body-cell-deps="{ row }">
            <q-td>
              <span v-if="!dependencyCount(row)" class="text-faint">{{ t('browse.galaxy.noDependencies') }}</span>
              <span v-else class="mono deps">{{ dependencyText(row) }}</span>
            </q-td>
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
      v-else-if="namespace"
      class="ws-table"
      flat
      :rows="collections.data.value?.items ?? []"
      :columns="collectionColumns"
      row-key="name"
      :loading="collections.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: GalaxyCollection) => (collection = row.name)"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <div class="mono coll-name">{{ row.name }}</div>
          <div v-if="row.description" class="coll-desc">{{ row.description }}</div>
        </q-td>
      </template>
    </q-table>

    <q-table
      v-else
      class="ws-table"
      flat
      :rows="namespaces.data.value?.items ?? []"
      :columns="namespaceColumns"
      row-key="namespace"
      :loading="namespaces.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: GalaxyNamespace) => open(row.namespace)"
    >
      <template #body-cell-namespace="{ row }">
        <q-td class="cursor-pointer mono coll-name">{{ row.namespace }}</q-td>
      </template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { GalaxyCollection, GalaxyNamespace, GalaxyVersion, Repository } from '@/api/types'
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
const namespace = useQueryParam('namespace')
const collection = useQueryParam('collection')
const search = ref<string | null>(null)

const repoUrl = computed(() => props.repo.url)

const namespaces = useAsync(
  async () =>
    namespace.value
      ? null
      : call(api.GET('/repositories/{name}/galaxy/namespaces', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [namespace, search],
)
const collections = useAsync(
  async () =>
    namespace.value && !collection.value
      ? call(
          api.GET('/repositories/{name}/galaxy/collections', {
            params: { path: { name: props.repo.name }, query: { namespace: namespace.value, q: search.value || undefined, limit: 200 } },
          }),
        )
      : null,
  [namespace, collection, search],
)
const versions = useAsync(
  async () =>
    namespace.value && collection.value
      ? call(
          api.GET('/repositories/{name}/galaxy/versions', {
            params: { path: { name: props.repo.name }, query: { namespace: namespace.value, collection: collection.value, limit: 200 } },
          }),
        )
      : null,
  [namespace, collection],
)

watch(namespace, () => (search.value = null))

function open(ns: string): void {
  collection.value = null
  namespace.value = ns
}

function go(index: number): void {
  if (index === 0) {
    collection.value = null
    namespace.value = null
  } else {
    collection.value = null
  }
}

const crumbs = computed(() => [
  { label: t('browse.galaxy.namespaces') },
  ...(namespace.value ? [{ label: namespace.value, mono: true }] : []),
  ...(namespace.value && collection.value ? [{ label: collection.value, mono: true }] : []),
])

function dependencyCount(row: GalaxyVersion): number {
  return Object.keys(row.dependencies ?? {}).length
}

function dependencyText(row: GalaxyVersion): string {
  return Object.entries(row.dependencies ?? {})
    .map(([name, range]) => `${name} ${range}`)
    .join(', ')
}

const namespaceColumns = computed<QTableColumn<GalaxyNamespace>[]>(() => [
  { name: 'namespace', label: t('browse.galaxy.namespace'), field: 'namespace', align: 'left', sortable: true },
  { name: 'collections', label: t('browse.galaxy.collections'), field: 'collection_count', align: 'right', sortable: true },
])

const collectionColumns = computed<QTableColumn<GalaxyCollection>[]>(() => [
  { name: 'name', label: t('browse.galaxy.collection'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.galaxy.latest'), field: 'latest_version', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'requires', label: t('browse.galaxy.requiresAnsible'), field: (r) => r.requires_ansible ?? '', align: 'left', classes: 'mono gt-sm', headerClasses: 'gt-sm' },
  { name: 'versions', label: t('browse.galaxy.versions'), field: 'version_count', align: 'right', sortable: true },
])

const columns = computed<QTableColumn<GalaxyVersion>[]>(() => [
  { name: 'version', label: t('browse.galaxy.version'), field: 'version', align: 'left' },
  { name: 'requires', label: t('browse.galaxy.requiresAnsible'), field: (r) => r.requires_ansible ?? '', align: 'left', classes: 'mono gt-sm', headerClasses: 'gt-sm' },
  { name: 'created', label: t('browse.galaxy.created'), field: (r) => r.created_at ?? '', align: 'left' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size_bytes ?? 0, align: 'right' },
  { name: 'sha', label: t('browse.galaxy.checksum'), field: (r) => r.sha256 ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'deps', label: t('browse.galaxy.dependencies'), field: (r) => dependencyCount(r), align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
  { name: 'by', label: t('browse.galaxy.uploadedBy'), field: (r) => r.uploaded_by ?? '', align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
  { name: 'cached', label: t('browse.cache'), field: 'cached', align: 'left' },
])
</script>

<style scoped>
.browse-search {
  width: 260px;
}

.coll-name {
  font-weight: 600;
}

.coll-desc {
  max-width: 520px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.coll__head {
  display: flex;
  flex-wrap: wrap;
  gap: 16px 24px;
  padding: 16px;
  border-top: 1px solid var(--border);
}

.coll__name {
  font-size: var(--fs-16);
  font-weight: 600;
}

.coll__desc {
  margin-top: 4px;
  font-size: var(--fs-14);
}

.coll__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin-top: 6px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.coll__install {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 280px;
}

.ver {
  font-weight: 600;
}

.badge {
  margin-left: 8px;
  height: 18px;
  font-size: 11px;
}

.digest {
  color: var(--text-secondary);
}

.deps {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
