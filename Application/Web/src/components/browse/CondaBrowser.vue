<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Conda browse: package names → package page (install line, files newest version first by conda version order, per
     subdir: build, build number, size, sha256, verified against the repodata listing or not, held or not; hosted:
     dependencies and who uploaded each file). Conda repositories have no groups. -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="pkg = null" /></template>
    <template v-if="!pkg" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.conda.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>
    <template v-else-if="files.data.value && files.data.value.package.subdirs.length > 1" #actions>
      <q-select v-model="subdir" dense outlined emit-value map-options clearable :options="subdirOptions" :label="t('browse.conda.subdir')" class="browse-search" />
    </template>

    <template v-if="pkg">
      <div v-if="files.data.value" class="pkg">
        <div class="pkg__head">
          <div class="col">
            <div class="pkg__name mono">{{ files.data.value.package.name }}</div>
            <div v-if="files.data.value.package.summary" class="pkg__desc">{{ files.data.value.package.summary }}</div>
            <div class="pkg__meta">
              <span v-if="files.data.value.package.latest">{{ t('browse.conda.latest') }} <strong class="mono">{{ files.data.value.package.latest }}</strong></span>
              <span>{{ t('browse.conda.fileCount', { count: files.data.value.package.file_count }) }}</span>
              <span class="mono">{{ files.data.value.package.subdirs.join(', ') }}</span>
              <span v-if="files.data.value.package.license">{{ files.data.value.package.license }}</span>
            </div>
          </div>
          <div v-if="files.data.value.package.latest" class="pkg__install">
            <div class="section-label">{{ t('browse.conda.install') }}</div>
            <CopyField :value="installLine(files.data.value.package.name, files.data.value.package.latest)" />
          </div>
        </div>

        <q-table class="ws-table" flat :rows="files.data.value.items" :columns="columns" row-key="filename" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('browse.empty')">
          <template #body-cell-version="{ row }">
            <q-td>
              <span class="mono ver">{{ row.version }}</span>
              <span v-if="row.prerelease" class="ws-badge tone-offline badge">{{ t('browse.conda.prerelease') }}</span>
              <span v-if="!row.verified" class="ws-badge tone-degraded badge">{{ t('browse.conda.unverified') }}<q-tooltip max-width="320px">{{ t('browse.conda.unverifiedHelp') }}</q-tooltip></span>
            </q-td>
          </template>
          <template #body-cell-build="{ row }">
            <q-td>
              <span class="mono">{{ row.build }}</span>
              <q-tooltip v-if="row.depends.length" max-width="420px" class="mono">{{ row.depends.join('\n') }}</q-tooltip>
            </q-td>
          </template>
          <template #body-cell-when="{ row }">
            <q-td><TimeCell :value="(hosted ? row.cached_at : row.timestamp) ?? null" placeholder="" /></q-td>
          </template>
          <template #body-cell-held="{ row }">
            <q-td>
              <CacheBadge v-if="row.cached" state="HIT" />
              <span v-else class="text-faint">{{ t('browse.notCached') }}</span>
            </q-td>
          </template>
          <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size ?? null" /></q-td></template>
          <template #body-cell-sha256="{ row }">
            <q-td class="digest">
              <template v-if="row.sha256">
                <span class="mono">{{ row.sha256.slice(0, 12) }}</span>
                <q-tooltip>sha256:{{ row.sha256 }}</q-tooltip>
                <q-btn flat dense round size="xs" :icon="iconCopy" :aria-label="t('browse.conda.copyChecksum')" @click.stop="copy(`sha256:${row.sha256}`, t('browse.conda.copyChecksum'))" />
              </template>
              <span v-else class="text-faint">md5</span>
            </q-td>
          </template>
        </q-table>
      </div>
      <div v-else-if="files.error.value" class="q-pa-md text-negative">{{ errorMessage(files.error.value) }}</div>
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
      @row-click="(_e: Event, row: CondaPackage) => (pkg = row.name)"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <div class="mono pkg-name">{{ row.name }}</div>
          <div v-if="row.summary" class="pkg-desc">{{ row.summary }}</div>
        </q-td>
      </template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { CondaFile, CondaPackage, Repository } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
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
const pkg = useQueryParam('package')
const search = ref<string | null>(null)
const subdir = ref<string | null>(null)
const hosted = computed(() => props.repo.type === 'hosted')
watch(pkg, () => (subdir.value = null))

const packages = useAsync(
  async () => call(api.GET('/repositories/{name}/conda/packages', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const files = useAsync(
  async () =>
    pkg.value
      ? call(api.GET('/repositories/{name}/conda/files', { params: { path: { name: props.repo.name }, query: { package: pkg.value, subdir: subdir.value || undefined, limit: 200 } } }))
      : null,
  [pkg, subdir],
)

const crumbs = computed(() => [{ label: t('browse.conda.packages') }, ...(pkg.value ? [{ label: files.data.value?.package.name ?? pkg.value, mono: true }] : [])])
const subdirOptions = computed(() => (files.data.value?.package.subdirs ?? []).map((s) => ({ label: s, value: s })))

function installLine(name: string, version: string): string {
  return `conda install -c ${window.location.origin}/repository/${props.repo.name} ${name}=${version}`
}

const packageColumns = computed<QTableColumn<CondaPackage>[]>(() => [
  { name: 'name', label: t('browse.conda.package'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.conda.latest'), field: (r) => r.latest ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'subdirs', label: t('browse.conda.subdirs'), field: (r) => r.subdirs.join(', '), align: 'left', classes: 'mono gt-sm', headerClasses: 'gt-sm' },
  { name: 'files', label: t('browse.conda.files'), field: 'file_count', align: 'right', sortable: true },
  { name: 'license', label: t('browse.conda.license'), field: (r) => r.license ?? '', align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
])

const columns = computed<QTableColumn<CondaFile>[]>(() => [
  { name: 'version', label: t('browse.conda.version'), field: 'version', align: 'left' },
  { name: 'build', label: t('browse.conda.build'), field: 'build', align: 'left' },
  { name: 'subdir', label: t('browse.conda.subdir'), field: 'subdir', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'when', label: hosted.value ? t('browse.conda.uploaded') : t('browse.conda.built'), field: (r) => (hosted.value ? r.cached_at : r.timestamp) ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  ...(hosted.value
    ? [{ name: 'by', label: t('browse.conda.uploadedBy'), field: (r: CondaFile) => r.uploaded_by ?? '', align: 'left' as const, classes: 'gt-md', headerClasses: 'gt-md' }]
    : [{ name: 'held', label: t('browse.conda.held'), field: 'cached' as const, align: 'left' as const }]),
  { name: 'size', label: t('browse.size'), field: (r) => r.size ?? 0, align: 'right' },
  { name: 'sha256', label: t('browse.conda.checksum'), field: (r) => r.sha256 ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
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

.badge {
  margin-left: 8px;
  height: 18px;
  font-size: 11px;
}

.digest {
  color: var(--text-secondary);
  font-size: var(--fs-12);
  white-space: nowrap;
}
</style>
