<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Terraform browse: providers (by address) → provider page (required_providers snippet, versions newest first with
     their protocols and platforms; the archives held per platform with sha256 and h1 hashes). -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="provider = null" /></template>
    <template v-if="!provider" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.terraform.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="provider">
      <div v-if="versions.data.value" class="pkg">
        <div class="pkg__head">
          <div class="col">
            <div class="pkg__name mono">{{ versions.data.value.provider.provider }}</div>
            <div class="pkg__meta">
              <span v-if="versions.data.value.provider.latest">{{ t('browse.terraform.latest') }} <strong class="mono">{{ versions.data.value.provider.latest }}</strong></span>
              <span>{{ t('browse.terraform.versionCount', { count: versions.data.value.provider.version_count }) }}</span>
              <span>{{ t('browse.terraform.archivesCached', { count: versions.data.value.provider.cached_archives }) }}</span>
              <span><SizeCell :bytes="versions.data.value.provider.size_bytes" /></span>
            </div>
          </div>
          <div v-if="versions.data.value.provider.latest" class="pkg__install">
            <div class="section-label">{{ t('browse.terraform.use') }}</div>
            <CopyField :value="requiredProviders(versions.data.value.provider.provider, versions.data.value.provider.latest)" />
          </div>
        </div>

        <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" row-key="version" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('browse.empty')">
          <template #body="scope">
            <q-tr :props="scope" class="cursor-pointer" @click="toggle(scope.row.version)">
              <q-td key="version" :props="scope">
                <q-icon :name="iconChevronRight" size="16px" class="chev" :class="{ 'chev--open': expanded.has(scope.row.version) }" />
                <span class="mono ver">{{ scope.row.version }}</span>
                <span v-if="scope.row.prerelease" class="ws-badge tone-offline badge">{{ t('browse.terraform.prerelease') }}</span>
                <span v-if="!scope.row.listed" class="ws-badge tone-offline badge">{{ t('browse.terraform.unlisted') }}<q-tooltip>{{ t('browse.terraform.unlistedHelp') }}</q-tooltip></span>
              </q-td>
              <q-td key="protocols" :props="scope"><span class="mono text-secondary">{{ scope.row.protocols.join(', ') }}</span></q-td>
              <q-td key="platforms" :props="scope">{{ t('browse.terraform.platformCount', { count: scope.row.platforms.length }) }}</q-td>
              <q-td key="cached" :props="scope">
                <CacheBadge v-if="scope.row.archives.length" state="HIT" />
                <span v-else class="text-faint">{{ t('browse.notCached') }}</span>
              </q-td>
              <q-td key="size" :props="scope" class="text-right"><SizeCell :bytes="scope.row.size_bytes" /></q-td>
            </q-tr>
            <q-tr v-if="expanded.has(scope.row.version)" :props="scope" class="files-row">
              <q-td colspan="100%">
                <div v-if="!scope.row.archives.length" class="text-faint">{{ t('browse.terraform.noArchives') }}</div>
                <div v-for="a in scope.row.archives" :key="`${a.os}_${a.arch}`" class="file">
                  <span class="ws-badge tone-offline kind mono">{{ a.os }}_{{ a.arch }}</span>
                  <span class="file__size"><SizeCell :bytes="a.size" /></span>
                  <span class="file__when"><TimeCell :value="a.cached_at ?? null" placeholder="" /></span>
                  <span class="file__sha"><CopyField compact :value="`zh:${a.sha256}`" :label="t('browse.terraform.copySha')" /></span>
                  <span v-if="a.h1" class="file__sha"><CopyField compact :value="a.h1" :label="t('browse.terraform.copyH1')" /></span>
                </div>
                <div v-if="scope.row.platforms.length" class="platforms">
                  <span class="section-label">{{ t('browse.terraform.platforms') }}</span>
                  <span v-for="p in scope.row.platforms" :key="p" class="platform mono">{{ p }}</span>
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
      :rows="providers.data.value?.items ?? []"
      :columns="providerColumns"
      row-key="provider"
      :loading="providers.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: TerraformProvider) => (provider = row.provider)"
    >
      <template #body-cell-provider="{ row }">
        <q-td class="cursor-pointer"><span class="mono pkg-name">{{ row.provider }}</span></q-td>
      </template>
      <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td></template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { Repository, TerraformProvider, TerraformVersion } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { iconChevronRight, iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const provider = useQueryParam('provider')
const search = ref<string | null>(null)
const expanded = ref(new Set<string>())

const providers = useAsync(
  async () => call(api.GET('/repositories/{name}/terraform/providers', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const versions = useAsync(
  async () =>
    provider.value ? call(api.GET('/repositories/{name}/terraform/versions', { params: { path: { name: props.repo.name }, query: { provider: provider.value, limit: 200 } } })) : null,
  [provider],
)

watch(provider, () => {
  expanded.value = new Set()
})
watch(
  () => versions.data.value,
  (page) => {
    // open the newest version with a cached archive so its hashes show without a click
    const first = page?.items.find((v) => v.archives.length) ?? page?.items[0]
    if (first && !expanded.value.size) expanded.value = new Set([first.version])
  },
)

const crumbs = computed(() => [{ label: t('browse.terraform.providers') }, ...(provider.value ? [{ label: provider.value, mono: true }] : [])])

/** A required_providers entry for the provider (the local name is the type). */
function requiredProviders(address: string, version: string): string {
  const parts = address.split('/')
  const source = parts[0] === 'registry.terraform.io' || parts[0] === 'registry.opentofu.org' ? parts.slice(1).join('/') : address
  return `${parts[2]} = { source = "${source}", version = "${version}" }`
}

function toggle(version: string): void {
  const next = new Set(expanded.value)
  if (next.has(version)) next.delete(version)
  else next.add(version)
  expanded.value = next
}

const providerColumns = computed<QTableColumn<TerraformProvider>[]>(() => [
  { name: 'provider', label: t('browse.terraform.provider'), field: 'provider', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.terraform.latest'), field: (r) => r.latest ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'versions', label: t('browse.terraform.versions'), field: 'version_count', align: 'right', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'cached', label: t('browse.terraform.archives'), field: 'cached_archives', align: 'right', sortable: true },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right', sortable: true },
])

const columns = computed<QTableColumn<TerraformVersion>[]>(() => [
  { name: 'version', label: t('browse.terraform.version'), field: 'version', align: 'left' },
  { name: 'protocols', label: t('browse.terraform.protocols'), field: (r) => r.protocols.join(', '), align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'platforms', label: t('browse.terraform.platforms'), field: (r) => r.platforms.length, align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'cached', label: t('browse.terraform.archives'), field: (r) => r.archives.length, align: 'left' },
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
  flex: 1 1 240px;
  min-width: 0;
  color: var(--text-secondary);
  font-size: var(--fs-12);
}

.kind {
  height: 18px;
  font-size: 11px;
  min-width: 96px;
  justify-content: center;
}

.platforms {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 10px 0 4px 22px;
}

.platform {
  padding: 1px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
