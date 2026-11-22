<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Alpine apk browse: packages (name, description, newest version, architectures) → package page (apk add snippet,
     files newest first in apk version order: version, architecture, branch/repository, build time, license,
     dependencies, size, the APKINDEX C: checksum, verified and cached state, download link; hosted: who uploaded it).
     "Files" switches to the folder tree. -->
<template>
  <div class="apk">
    <q-btn-toggle
      v-model="view"
      no-caps
      dense
      unelevated
      toggle-color="primary"
      class="seg apk__view"
      :options="[
        { label: t('browse.apk.packages'), value: 'packages' },
        { label: t('browse.apk.files'), value: 'files' },
      ]"
    />
    <FolderBrowser v-if="view === 'files'" :repo="repo" />
    <PageCard v-else flush>
      <template #title><BrowseCrumbs :crumbs="crumbs" @go="pkg = null" /></template>
      <template v-if="!pkg" #actions>
        <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.apk.search')" class="browse-search">
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
                <span v-if="versions.data.value.package.latest_version">{{ t('browse.apk.latest') }} <strong class="mono">{{ versions.data.value.package.latest_version }}</strong></span>
                <span>{{ t('browse.apk.arches') }} <span class="mono">{{ versions.data.value.package.arches.join(', ') }}</span></span>
                <span>{{ t('browse.apk.versionCount', { count: versions.data.value.package.version_count }) }}</span>
                <span v-if="versions.data.value.package.origin && versions.data.value.package.origin !== versions.data.value.package.name">
                  {{ t('browse.apk.origin') }} <span class="mono">{{ versions.data.value.package.origin }}</span>
                </span>
              </div>
            </div>
            <div class="pkg__install">
              <div class="section-label">{{ t('browse.apk.install') }}</div>
              <CopyField :value="`apk add ${versions.data.value.package.name}`" />
            </div>
          </div>
          <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" row-key="path" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('browse.empty')">
            <template #body-cell-version="{ row }">
              <q-td>
                <span class="mono ver">{{ row.version }}</span>
                <div v-if="row.uploaded_by" class="by">{{ t('browse.apk.uploadedBy', { user: row.uploaded_by }) }}</div>
              </q-td>
            </template>
            <template #body-cell-arch="{ row }"><q-td><span class="ws-badge">{{ row.arch }}</span></q-td></template>
            <template #body-cell-repository="{ row }">
              <q-td class="mono text-secondary">{{ row.branch }}/{{ row.repository }}<q-tooltip>{{ row.directory }}</q-tooltip></q-td>
            </template>
            <template #body-cell-built="{ row }"><q-td><TimeCell :value="row.build_time ?? row.uploaded_at" placeholder="" /></q-td></template>
            <template #body-cell-license="{ row }"><q-td class="license">{{ row.license ?? '' }}</q-td></template>
            <template #body-cell-dependencies="{ row }">
              <q-td>
                <span v-if="row.depends.length" class="deps">
                  {{ t('browse.apk.dependencyCount', { count: row.depends.length }) }}
                  <q-tooltip max-width="420px">
                    <div v-for="d in row.depends" :key="d" class="mono">{{ d }}</div>
                  </q-tooltip>
                </span>
                <span v-else class="text-faint">{{ t('browse.apk.noDependencies') }}</span>
              </q-td>
            </template>
            <template #body-cell-size="{ row }">
              <q-td class="text-right">
                <SizeCell :bytes="row.size_bytes" />
                <div v-if="row.installed_size" class="by">{{ t('browse.apk.installedSize', { size: formatBytes(row.installed_size) }) }}</div>
              </q-td>
            </template>
            <template #body-cell-checksum="{ row }">
              <q-td>
                <template v-if="row.checksum">
                  <span class="mono digest">{{ row.checksum.slice(0, 14) }}<q-tooltip max-width="360px">{{ row.checksum }}<br />{{ t('browse.apk.checksumHelp') }}</q-tooltip></span>
                  <q-btn flat dense round size="xs" :icon="iconCopy" :aria-label="t('browse.apk.copyChecksum')" @click="copy(row.checksum, t('browse.apk.checksumLabel'))">
                    <q-tooltip>{{ t('browse.apk.copyChecksum') }}</q-tooltip>
                  </q-btn>
                </template>
              </q-td>
            </template>
            <template #body-cell-verified="{ row }">
              <q-td>
                <span v-if="row.verified" class="ws-badge tone-ok">{{ t('browse.apk.verified') }}<q-tooltip max-width="320px">{{ t('browse.apk.verifiedHelp') }}</q-tooltip></span>
                <span v-else class="ws-badge tone-degraded">{{ t('browse.apk.unverified') }}<q-tooltip max-width="320px">{{ t('browse.apk.unverifiedHelp') }}</q-tooltip></span>
              </q-td>
            </template>
            <template #body-cell-cached="{ row }">
              <q-td><CacheBadge v-if="row.cached" state="HIT" /><span v-else class="text-faint">{{ t('browse.notCached') }}</span></q-td>
            </template>
            <template #body-cell-actions="{ row }">
              <q-td auto-width class="actions">
                <q-btn flat dense round size="sm" :icon="iconCopy" :aria-label="t('browse.copyUrl')" @click="copy(row.download_url, t('browse.url'))">
                  <q-tooltip>{{ t('browse.copyUrl') }}</q-tooltip>
                </q-btn>
                <q-btn v-if="row.cached" flat dense round size="sm" type="a" :href="row.download_url" :icon="iconDownload" :aria-label="t('browse.apk.download')">
                  <q-tooltip>{{ t('browse.apk.download') }}</q-tooltip>
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
        :no-data-label="t('browse.empty')"
        @row-click="(_e: Event, row: ApkPackage) => (pkg = row.name)"
      >
        <template #body-cell-name="{ row }">
          <q-td class="cursor-pointer">
            <div class="mono pkg-name">{{ row.name }}</div>
            <div v-if="row.description" class="pkg-desc">{{ row.description }}</div>
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
import type { ApkPackage, ApkVersion, Repository } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy } from '@/composables/useNotify'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { formatBytes } from '@/utils/format'
import { iconCopy, iconDownload, iconSearch } from '@/utils/icons'

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
  async () => (view.value === 'packages' ? call(api.GET('/repositories/{name}/apk/packages', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })) : null),
  [search, view],
)
const versions = useAsync(
  async () => (pkg.value && view.value === 'packages' ? call(api.GET('/repositories/{name}/apk/versions', { params: { path: { name: props.repo.name }, query: { package: pkg.value, limit: 200 } } })) : null),
  [pkg, view],
)

const crumbs = computed(() => [{ label: t('browse.apk.packages') }, ...(pkg.value ? [{ label: pkg.value, mono: true }] : [])])

const packageColumns = computed<QTableColumn<ApkPackage>[]>(() => [
  { name: 'name', label: t('browse.apk.package'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.apk.latest'), field: (r) => r.latest_version ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'arches', label: t('browse.apk.arches'), field: (r) => r.arches.join(' '), align: 'left', headerClasses: 'gt-xs' },
  { name: 'versions', label: t('browse.apk.versions'), field: 'version_count', align: 'right', sortable: true },
])

const columns = computed<QTableColumn<ApkVersion>[]>(() => [
  { name: 'version', label: t('browse.apk.version'), field: 'version', align: 'left' },
  { name: 'arch', label: t('browse.apk.arch'), field: 'arch', align: 'left' },
  { name: 'repository', label: t('browse.apk.repository'), field: (r) => `${r.branch}/${r.repository}`, align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'built', label: t('browse.apk.built'), field: (r) => r.build_time ?? r.uploaded_at ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'license', label: t('browse.apk.license'), field: (r) => r.license ?? '', align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
  { name: 'dependencies', label: t('browse.apk.dependencies'), field: (r) => r.depends.length, align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size_bytes ?? 0, align: 'right' },
  { name: 'checksum', label: t('browse.apk.checksum'), field: (r) => r.checksum ?? '', align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
  { name: 'verified', label: '', field: 'verified', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'cached', label: t('browse.cache'), field: 'cached', align: 'left' },
  { name: 'actions', label: '', field: 'path', align: 'right' },
])
</script>

<style scoped>
.apk {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.apk__view {
  align-self: flex-start;
}

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

.by {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.license {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.deps {
  cursor: default;
  border-bottom: 1px dotted var(--text-faint);
}

.digest {
  color: var(--text-secondary);
}

.actions {
  white-space: nowrap;
}
</style>
