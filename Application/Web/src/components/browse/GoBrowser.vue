<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Go browse: modules → module page (go get snippet, versions newest first: tagged, pre-release, pseudo-version,
     +incompatible; commit time and the .info/.mod/.zip files held). Groups are browsed through their members. -->
<template>
  <PageCard v-if="repo.type === 'group'" flush>
    <template #title><BrowseCrumbs :crumbs="[{ label: t('browse.go.modules') }]" /></template>
    <EmptyState compact illustration="repositories" :title="t('browse.go.groupTitle')" :text="t('browse.go.groupText')">
      <div class="members">
        <router-link v-for="m in repo.group?.members ?? []" :key="m" :to="{ name: 'repository', params: { name: m, tab: 'browse' } }" class="member mono">{{ m }}</router-link>
      </div>
    </EmptyState>
  </PageCard>

  <PageCard v-else flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="module = null" /></template>
    <template v-if="!module" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.go.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="module">
      <div v-if="versions.data.value" class="pkg">
        <div class="pkg__head">
          <div class="col">
            <div class="pkg__name mono">{{ versions.data.value.module.module }}</div>
            <div class="pkg__meta">
              <span v-if="versions.data.value.module.latest">{{ t('browse.go.latest') }} <strong class="mono">{{ versions.data.value.module.latest }}</strong></span>
              <span>{{ t('browse.go.versionCount', { count: versions.data.value.module.version_count }) }}</span>
              <span>{{ t('browse.go.cachedOf', { cached: versions.data.value.module.cached_versions, total: versions.data.value.items.length }) }}</span>
              <span><SizeCell :bytes="versions.data.value.module.size_bytes" /></span>
            </div>
          </div>
          <div v-if="versions.data.value.module.latest" class="pkg__install">
            <div class="section-label">{{ t('browse.go.get') }}</div>
            <CopyField :value="`go get ${versions.data.value.module.module}@${versions.data.value.module.latest}`" />
          </div>
        </div>

        <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" row-key="version" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('browse.empty')">
          <template #body="scope">
            <q-tr :props="scope" class="cursor-pointer" @click="toggle(scope.row.version)">
              <q-td key="version" :props="scope">
                <q-icon :name="iconChevronRight" size="16px" class="chev" :class="{ 'chev--open': expanded.has(scope.row.version) }" />
                <span class="mono ver">{{ scope.row.version }}</span>
                <span v-if="scope.row.pseudo" class="ws-badge tone-offline badge">{{ t('browse.go.pseudo') }}<q-tooltip>{{ t('browse.go.pseudoHelp') }}</q-tooltip></span>
                <span v-else-if="scope.row.prerelease" class="ws-badge tone-offline badge">{{ t('browse.go.prerelease') }}</span>
                <span v-if="scope.row.incompatible" class="ws-badge tone-degraded badge">{{ t('browse.go.incompatible') }}<q-tooltip>{{ t('browse.go.incompatibleHelp') }}</q-tooltip></span>
                <span v-if="!scope.row.listed && !scope.row.pseudo" class="ws-badge tone-offline badge">{{ t('browse.go.untagged') }}<q-tooltip>{{ t('browse.go.untaggedHelp') }}</q-tooltip></span>
              </q-td>
              <q-td key="time" :props="scope"><TimeCell :value="scope.row.time ?? null" placeholder="" /></q-td>
              <q-td key="cached" :props="scope">
                <CacheBadge v-if="scope.row.cached" state="HIT" />
                <span v-else class="text-faint">{{ t('browse.notCached') }}</span>
              </q-td>
              <q-td key="size" :props="scope" class="text-right"><SizeCell :bytes="scope.row.size_bytes" /></q-td>
            </q-tr>
            <q-tr v-if="expanded.has(scope.row.version)" :props="scope" class="files-row">
              <q-td colspan="100%">
                <div v-if="!scope.row.files.length" class="text-faint">{{ t('browse.go.noFiles') }}</div>
                <div v-for="f in scope.row.files" :key="f.kind" class="file">
                  <span class="ws-badge tone-offline kind">.{{ f.kind }}</span>
                  <span class="file__size"><SizeCell :bytes="f.size" /></span>
                  <span class="file__when"><TimeCell :value="f.cached_at ?? null" placeholder="" /></span>
                  <span class="file__sha"><CopyField compact :value="`sha256:${f.sha256}`" :label="t('browse.go.copySha')" /></span>
                </div>
                <div class="file file--get">
                  <CopyField :value="`go get ${versions.data.value.module.module}@${scope.row.version}`" />
                </div>
              </q-td>
            </q-tr>
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
      :rows="modules.data.value?.items ?? []"
      :columns="moduleColumns"
      row-key="module"
      :loading="modules.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: GoModule) => (module = row.module)"
    >
      <template #body-cell-module="{ row }">
        <q-td class="cursor-pointer"><span class="mono pkg-name">{{ row.module }}</span></q-td>
      </template>
      <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td></template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { GoModule, GoVersion, Repository } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { iconChevronRight, iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const module = useQueryParam('module')
const search = ref<string | null>(null)
const expanded = ref(new Set<string>())

const modules = useAsync(
  async () =>
    props.repo.type === 'group' ? null : call(api.GET('/repositories/{name}/go/modules', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const versions = useAsync(
  async () => (module.value ? call(api.GET('/repositories/{name}/go/versions', { params: { path: { name: props.repo.name }, query: { module: module.value, limit: 200 } } })) : null),
  [module],
)

watch(module, () => {
  expanded.value = new Set()
})
watch(
  () => versions.data.value,
  (page) => {
    // open the newest cached version so its files show without a click
    const first = page?.items.find((v) => v.cached) ?? page?.items[0]
    if (first && !expanded.value.size) expanded.value = new Set([first.version])
  },
)

const crumbs = computed(() => [{ label: t('browse.go.modules') }, ...(module.value ? [{ label: module.value, mono: true }] : [])])

function toggle(version: string): void {
  const next = new Set(expanded.value)
  if (next.has(version)) next.delete(version)
  else next.add(version)
  expanded.value = next
}

const moduleColumns = computed<QTableColumn<GoModule>[]>(() => [
  { name: 'module', label: t('browse.go.module'), field: 'module', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.go.latest'), field: (r) => r.latest ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'versions', label: t('browse.go.versions'), field: 'version_count', align: 'right', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'cached', label: t('browse.go.cached'), field: 'cached_versions', align: 'right', sortable: true },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right', sortable: true },
])

const columns = computed<QTableColumn<GoVersion>[]>(() => [
  { name: 'version', label: t('browse.go.version'), field: 'version', align: 'left' },
  { name: 'time', label: t('browse.go.time'), field: (r) => r.time ?? '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'cached', label: t('browse.go.files'), field: 'cached', align: 'left' },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right' },
])
</script>

<style scoped>
.browse-search {
  width: 260px;
}

.pkg-name {
  font-weight: 600;
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
  overflow-wrap: anywhere;
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
  max-width: 100%;
}

.ver {
  font-weight: 600;
}

.badge {
  margin-left: 8px;
  height: 18px;
  font-size: 11px;
}

.chev {
  margin-right: 4px;
  color: var(--text-tertiary);
  transition: transform var(--dur) var(--ease-out);
}

.chev--open {
  transform: rotate(90deg);
}

.files-row > td {
  background: var(--surface-sunken);
}

.file {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  padding: 4px 0 4px 22px;
  font-size: var(--fs-13);
}

.file--get {
  padding-top: 8px;
}

.file__size {
  width: 80px;
  text-align: right;
}

.file__when {
  width: 140px;
  color: var(--text-secondary);
  font-size: var(--fs-12);
}

.file__sha {
  flex: 1 1 280px;
  min-width: 0;
  color: var(--text-secondary);
  font-size: var(--fs-12);
}

.kind {
  height: 18px;
  font-size: 11px;
  min-width: 48px;
  justify-content: center;
}

.members {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 12px;
}

.member {
  padding: 2px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--q-primary);
  text-decoration: none;
}
</style>
