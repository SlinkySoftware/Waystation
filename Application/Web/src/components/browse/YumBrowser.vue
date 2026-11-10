<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- yum browse: packages (name, newest epoch:version-release, architectures) → package page (install snippet, files
     newest first with architecture, directory, checksum and cached flag). "Files" switches to the folder tree. -->
<template>
  <div class="yum">
    <q-btn-toggle
      v-model="view"
      no-caps
      dense
      unelevated
      toggle-color="primary"
      class="seg yum__view"
      :options="[
        { label: t('browse.yum.packages'), value: 'packages' },
        { label: t('browse.yum.files'), value: 'files' },
      ]"
    />
    <FolderBrowser v-if="view === 'files'" :repo="repo" />
    <PageCard v-else flush>
      <template #title><BrowseCrumbs :crumbs="crumbs" @go="pkg = null" /></template>
      <template v-if="!pkg" #actions>
        <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.yum.search')" class="browse-search">
          <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
        </q-input>
      </template>

      <template v-if="pkg">
        <div v-if="versions.data.value" class="pkg">
          <div class="pkg__head">
            <div class="col">
              <div v-if="versions.data.value.package.summary" class="pkg__desc">{{ versions.data.value.package.summary }}</div>
              <div class="pkg__meta">
                <span>{{ t('browse.yum.latest') }} <strong class="mono">{{ versions.data.value.package.latest_version }}</strong></span>
                <span>{{ t('browse.yum.arches') }} <span class="mono">{{ versions.data.value.package.arches.join(', ') }}</span></span>
                <span>{{ t('browse.yum.versionCount', { count: versions.data.value.package.version_count }) }}</span>
              </div>
            </div>
            <div class="pkg__install">
              <div class="section-label">{{ t('browse.yum.install') }}</div>
              <CopyField :value="`sudo dnf install ${pkg}`" />
            </div>
          </div>
          <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" row-key="path" :pagination="{ rowsPerPage: 0 }" hide-pagination>
            <template #body-cell-evr="{ row }"><q-td class="mono ver">{{ row.evr }}</q-td></template>
            <template #body-cell-arch="{ row }"><q-td><span class="ws-badge">{{ row.arch }}</span></q-td></template>
            <template #body-cell-directory="{ row }"><q-td class="mono text-secondary">{{ row.directory || '/' }}</q-td></template>
            <template #body-cell-uploaded="{ row }"><q-td><TimeCell :value="row.uploaded_at" placeholder="" /></q-td></template>
            <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td></template>
            <template #body-cell-checksum="{ row }">
              <q-td class="mono digest">{{ shortChecksum(row.checksum) }}<q-tooltip v-if="row.checksum">{{ row.checksum }}</q-tooltip></q-td>
            </template>
            <template #body-cell-cached="{ row }">
              <q-td><CacheBadge v-if="row.cached" state="HIT" /><span v-else class="text-faint">{{ t('browse.notCached') }}</span></q-td>
            </template>
            <template #body-cell-actions="{ row }">
              <q-td class="text-right">
                <q-btn flat dense round size="sm" :icon="iconCopy" :aria-label="t('browse.copyUrl')" @click="copy(row.download_url, t('browse.url'))">
                  <q-tooltip>{{ t('browse.copyUrl') }}</q-tooltip>
                </q-btn>
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
        :no-data-label="t('browse.yum.empty')"
        @row-click="(_e: Event, row: YumPackage) => (pkg = row.name)"
      >
        <template #body-cell-name="{ row }">
          <q-td class="cursor-pointer">
            <div class="mono pkg-name">{{ row.name }}</div>
            <div v-if="row.summary" class="pkg-desc">{{ row.summary }}</div>
          </q-td>
        </template>
        <template #body-cell-arches="{ row }">
          <q-td class="gt-xs"><span v-for="a in row.arches" :key="a" class="ws-badge arch">{{ a }}</span></q-td>
        </template>
      </q-table>
    </PageCard>
  </div>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { Repository, YumPackage, YumVersion } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy } from '@/composables/useNotify'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { iconCopy, iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'
import FolderBrowser from './FolderBrowser.vue'

const props = defineProps<{ repo: Repository }>()
const pkg = useQueryParam('package')
const viewParam = useQueryParam('view')
const view = computed<string>({
  get: () => (viewParam.value === 'files' ? 'files' : 'packages'),
  set: (v) => (viewParam.value = v === 'files' ? 'files' : null),
})
const search = ref<string | null>(null)
const copy = useCopy()

const packages = useAsync(
  async () => (view.value === 'packages' ? call(api.GET('/repositories/{name}/yum/packages', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })) : null),
  [search, view],
)
const versions = useAsync(
  async () => (pkg.value && view.value === 'packages' ? call(api.GET('/repositories/{name}/yum/versions', { params: { path: { name: props.repo.name }, query: { package: pkg.value, limit: 200 } } })) : null),
  [pkg, view],
)

const crumbs = computed(() => [{ label: t('browse.yum.packages') }, ...(pkg.value ? [{ label: pkg.value, mono: true }] : [])])

function shortChecksum(value: string | null | undefined): string {
  if (!value) return ''
  const [alg, hex] = value.split(':')
  return `${alg}:${(hex ?? '').slice(0, 12)}`
}

const packageColumns = computed<QTableColumn<YumPackage>[]>(() => [
  { name: 'name', label: t('browse.yum.package'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.yum.latest'), field: 'latest_version', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'arches', label: t('browse.yum.arches'), field: (r) => r.arches.join(' '), align: 'left', headerClasses: 'gt-xs' },
  { name: 'versions', label: t('browse.yum.versions'), field: 'version_count', align: 'right', sortable: true },
])

const columns = computed<QTableColumn<YumVersion>[]>(() => [
  { name: 'evr', label: t('browse.yum.version'), field: 'evr', align: 'left' },
  { name: 'arch', label: t('browse.yum.arch'), field: 'arch', align: 'left' },
  { name: 'directory', label: t('browse.yum.directory'), field: 'directory', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'uploaded', label: t('browse.yum.uploaded'), field: (r) => r.uploaded_at ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size_bytes ?? 0, align: 'right' },
  { name: 'checksum', label: t('browse.yum.checksum'), field: (r) => r.checksum ?? '', align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
  { name: 'cached', label: t('browse.cache'), field: 'cached', align: 'left' },
  { name: 'actions', label: '', field: 'path', align: 'right' },
])
</script>

<style scoped>
.yum {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.yum__view {
  align-self: flex-start;
}

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

.arch {
  margin-right: 4px;
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

.pkg__install {
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
</style>
