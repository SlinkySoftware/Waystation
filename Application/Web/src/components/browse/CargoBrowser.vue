<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Cargo browse: crates → crate page (Cargo.toml line, versions newest first by SemVer: yanked and pre-release, the
     .crate held or not, its checksum, rust-version, dependencies and features; hosted: who published it). Groups are
     browsed through their members. -->
<template>
  <PageCard v-if="repo.type === 'group'" flush>
    <template #title><BrowseCrumbs :crumbs="[{ label: t('browse.cargo.crates') }]" /></template>
    <EmptyState compact illustration="repositories" :title="t('browse.cargo.groupTitle')" :text="t('browse.cargo.groupText')">
      <div class="members">
        <router-link v-for="m in repo.group?.members ?? []" :key="m" :to="{ name: 'repository', params: { name: m, tab: 'packages' } }" class="member mono">{{ m }}</router-link>
      </div>
    </EmptyState>
  </PageCard>

  <PageCard v-else flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="crate = null" /></template>
    <template v-if="!crate" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.cargo.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="crate">
      <div v-if="versions.data.value" class="pkg">
        <div class="pkg__head">
          <div class="col">
            <div class="pkg__name mono">{{ versions.data.value.crate.name }}</div>
            <div v-if="versions.data.value.crate.description" class="pkg__desc">{{ versions.data.value.crate.description }}</div>
            <div class="pkg__meta">
              <span v-if="versions.data.value.crate.latest">{{ t('browse.cargo.latest') }} <strong class="mono">{{ versions.data.value.crate.latest }}</strong></span>
              <span>{{ t('browse.cargo.versionCount', { count: versions.data.value.crate.version_count }) }}</span>
              <span>{{ t(hosted ? 'browse.cargo.cratesStored' : 'browse.cargo.cratesCached', { count: versions.data.value.crate.cached_versions }) }}</span>
              <span><SizeCell :bytes="versions.data.value.crate.size_bytes" /></span>
            </div>
          </div>
          <div v-if="versions.data.value.crate.latest" class="pkg__install">
            <div class="section-label">{{ t('browse.cargo.dependency') }}</div>
            <CopyField :value="dependencyLine(versions.data.value.crate.name, versions.data.value.crate.latest)" />
          </div>
        </div>

        <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" row-key="version" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('browse.empty')">
          <template #body-cell-version="{ row }">
            <q-td>
              <span class="mono ver" :class="{ 'ver--yanked': row.yanked }">{{ row.version }}</span>
              <span v-if="row.yanked" class="ws-badge tone-degraded badge">{{ t('browse.cargo.yanked') }}<q-tooltip>{{ t('browse.cargo.yankedHelp') }}</q-tooltip></span>
              <span v-if="row.prerelease" class="ws-badge tone-offline badge">{{ t('browse.cargo.prerelease') }}</span>
            </q-td>
          </template>
          <template #body-cell-published="{ row }">
            <q-td><TimeCell :value="(hosted ? row.published_at : row.cached_at) ?? null" placeholder="" /></q-td>
          </template>
          <template #body-cell-held="{ row }">
            <q-td>
              <CacheBadge v-if="row.cached" state="HIT" />
              <span v-else class="text-faint">{{ t('browse.notCached') }}</span>
            </q-td>
          </template>
          <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size ?? null" /></q-td></template>
          <template #body-cell-features="{ row }">
            <q-td>
              <span v-if="!row.features.length" class="text-faint">{{ t('browse.cargo.noFeatures') }}</span>
              <span v-else class="features">
                <span v-for="f in row.features.slice(0, FEATURES_SHOWN)" :key="f" class="ws-badge tone-offline feature mono">{{ f }}</span>
                <span v-if="row.features.length > FEATURES_SHOWN" class="text-faint more">
                  {{ t('browse.cargo.moreFeatures', { n: row.features.length - FEATURES_SHOWN }) }}
                  <q-tooltip max-width="420px" class="mono">{{ row.features.join(', ') }}</q-tooltip>
                </span>
              </span>
            </q-td>
          </template>
          <template #body-cell-cksum="{ row }">
            <q-td class="digest">
              <span class="mono">{{ row.cksum.slice(0, 12) }}</span>
              <q-tooltip>sha256:{{ row.cksum }}</q-tooltip>
              <q-btn flat dense round size="xs" :icon="iconCopy" :aria-label="t('browse.cargo.copyChecksum')" @click.stop="copy(`sha256:${row.cksum}`, t('browse.cargo.copyChecksum'))" />
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
      :rows="crates.data.value?.items ?? []"
      :columns="crateColumns"
      row-key="name"
      :loading="crates.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: CargoCrate) => (crate = row.name)"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <div class="mono pkg-name">{{ row.name }}</div>
          <div v-if="row.description" class="pkg-desc">{{ row.description }}</div>
        </q-td>
      </template>
      <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td></template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { CargoCrate, CargoVersion, Repository } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy } from '@/composables/useNotify'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconCopy, iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const copy = useCopy()
const FEATURES_SHOWN = 3
const crate = useQueryParam('crate')
const search = ref<string | null>(null)
const hosted = computed(() => props.repo.type === 'hosted')

const crates = useAsync(
  async () =>
    props.repo.type === 'group' ? null : call(api.GET('/repositories/{name}/cargo/crates', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const versions = useAsync(
  async () => (crate.value ? call(api.GET('/repositories/{name}/cargo/versions', { params: { path: { name: props.repo.name }, query: { crate: crate.value, limit: 200 } } })) : null),
  [crate],
)

const crumbs = computed(() => [{ label: t('browse.cargo.crates') }, ...(crate.value ? [{ label: versions.data.value?.crate.name ?? crate.value, mono: true }] : [])])

/** The Cargo.toml line: a proxy replaces crates.io (plain version); a hosted registry is named. */
function dependencyLine(name: string, version: string): string {
  return hosted.value ? `${name} = { version = "${version}", registry = "waystation" }` : `${name} = "${version}"`
}

const crateColumns = computed<QTableColumn<CargoCrate>[]>(() => [
  { name: 'name', label: t('browse.cargo.crate'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.cargo.latest'), field: (r) => r.latest ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'versions', label: t('browse.cargo.versions'), field: 'version_count', align: 'right', sortable: true, classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'cached', label: hosted.value ? t('browse.cargo.stored') : t('browse.cargo.cached'), field: 'cached_versions', align: 'right', sortable: true },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right', sortable: true },
])

const columns = computed<QTableColumn<CargoVersion>[]>(() => [
  { name: 'version', label: t('browse.cargo.version'), field: 'version', align: 'left' },
  { name: 'published', label: hosted.value ? t('browse.cargo.published') : t('browse.cargo.cachedAt'), field: (r) => (hosted.value ? r.published_at : r.cached_at) ?? '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  ...(hosted.value
    ? [{ name: 'by', label: t('browse.cargo.publishedBy'), field: (r: CargoVersion) => r.published_by ?? '', align: 'left' as const, classes: 'gt-md', headerClasses: 'gt-md' }]
    : [{ name: 'held', label: t('browse.cargo.held'), field: 'cached' as const, align: 'left' as const }]),
  { name: 'size', label: t('browse.size'), field: (r) => r.size ?? 0, align: 'right' },
  { name: 'rust', label: t('browse.cargo.rustVersion'), field: (r) => r.rust_version ?? '', align: 'left', classes: 'mono gt-sm', headerClasses: 'gt-sm' },
  { name: 'deps', label: t('browse.cargo.dependencies'), field: 'dependencies', align: 'right', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'features', label: t('browse.cargo.features'), field: (r) => r.features.length, align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
  { name: 'cksum', label: t('browse.cargo.checksum'), field: 'cksum', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'downloads', label: t('browse.downloads'), field: 'downloads', format: (v: number) => formatCount(v), align: 'right', classes: 'gt-xs', headerClasses: 'gt-xs' },
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

.pkg__name {
  font-size: var(--fs-16);
  font-weight: 650;
  overflow-wrap: anywhere;
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
  max-width: 100%;
}

.ver {
  font-weight: 600;
}

.ver--yanked {
  color: var(--text-tertiary);
  text-decoration: line-through;
}

.badge {
  margin-left: 8px;
  height: 18px;
  font-size: 11px;
}

.features {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}

.feature {
  height: 18px;
  font-size: 11px;
}

.more {
  font-size: var(--fs-12);
}

.digest {
  color: var(--text-secondary);
  font-size: var(--fs-12);
  white-space: nowrap;
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
