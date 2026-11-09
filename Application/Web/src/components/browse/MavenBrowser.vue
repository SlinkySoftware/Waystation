<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Maven browse: artifacts (groupId:artifactId) → versions (Maven order, newest first) → the version's files. -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="goTo" /></template>
    <template v-if="!coords" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.maven.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="coords && version">
      <q-table
        class="ws-table"
        flat
        :rows="files.data.value?.entries.filter((e) => e.kind === 'file') ?? []"
        :columns="fileColumns"
        row-key="path"
        :loading="files.loading.value"
        :pagination="{ rowsPerPage: 0 }"
        hide-pagination
        :no-data-label="t('browse.emptyFolder')"
      >
        <template #body-cell-name="{ row }">
          <q-td><span class="entry"><q-icon :name="iconFile" size="18px" class="text-faint" /><span class="mono">{{ row.name }}</span></span></q-td>
        </template>
        <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size" /></q-td></template>
        <template #body-cell-modified="{ row }"><q-td><TimeCell :value="row.last_modified" /></q-td></template>
        <template #body-cell-actions="{ row }">
          <q-td class="text-right">
            <q-btn flat dense round size="sm" :icon="iconCopy" :aria-label="t('browse.copyUrl')" @click="copy(`${repo.url}${row.path}`, t('browse.url'))">
              <q-tooltip>{{ t('browse.copyUrl') }}</q-tooltip>
            </q-btn>
          </q-td>
        </template>
      </q-table>
    </template>

    <template v-else-if="coords">
      <div v-if="versions.data.value" class="pkg">
        <div class="pkg__head">
          <div class="col">
            <div class="pkg__meta">
              <span>{{ t('browse.maven.groupId') }} <strong class="mono">{{ coords.groupId }}</strong></span>
              <span>{{ t('browse.maven.artifactId') }} <strong class="mono">{{ coords.artifactId }}</strong></span>
            </div>
          </div>
          <div v-if="newest" class="pkg__install">
            <div class="section-label">{{ t('browse.maven.dependency') }}</div>
            <CopyField :value="dependency(newest)" />
          </div>
        </div>
        <q-table
          class="ws-table"
          flat
          :rows="versions.data.value.items"
          :columns="versionColumns"
          row-key="version"
          :pagination="{ rowsPerPage: 0 }"
          hide-pagination
          @row-click="(_e: Event, row: MavenVersion) => (version = row.version)"
        >
          <template #body-cell-version="{ row }">
            <q-td class="cursor-pointer">
              <span class="mono ver">{{ row.version }}</span>
              <span v-if="row.snapshot" class="ws-badge tone-stale snap">SNAPSHOT</span>
            </q-td>
          </template>
          <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td></template>
          <template #body-cell-updated="{ row }"><q-td><TimeCell :value="row.updated_at" /></q-td></template>
        </q-table>
      </div>
      <div v-else-if="versions.error.value" class="q-pa-md text-negative">{{ errorMessage(versions.error.value) }}</div>
      <q-skeleton v-else height="300px" class="q-ma-md" />
    </template>

    <q-table
      v-else
      class="ws-table"
      flat
      :rows="artifacts.data.value?.items ?? []"
      :columns="artifactColumns"
      :row-key="(r: MavenArtifact) => `${r.group_id}:${r.artifact_id}`"
      :loading="artifacts.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: MavenArtifact) => (ga = `${row.group_id}:${row.artifact_id}`)"
    >
      <template #body-cell-artifact="{ row }">
        <q-td class="cursor-pointer">
          <div class="mono pkg-name">{{ row.artifact_id }}</div>
          <div class="pkg-desc mono">{{ row.group_id }}</div>
        </q-td>
      </template>
      <template #body-cell-updated="{ row }"><q-td><TimeCell :value="row.updated_at" /></q-td></template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { FolderEntry, MavenArtifact, MavenVersion, Repository } from '@/api/types'
import CopyField from '@/components/CopyField.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy } from '@/composables/useNotify'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { iconCopy, iconFile, iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const ga = useQueryParam('artifact')
const version = useQueryParam('version')
const search = ref<string | null>(null)
const copy = useCopy()

/** `groupId:artifactId` from the query (the artifactId never contains ':'). */
const coords = computed(() => {
  const value = ga.value
  const colon = value ? value.lastIndexOf(':') : -1
  return value && colon > 0 ? { groupId: value.slice(0, colon), artifactId: value.slice(colon + 1) } : null
})

const artifacts = useAsync(
  () => call(api.GET('/repositories/{name}/maven/artifacts', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const versions = useAsync(
  async () =>
    coords.value
      ? call(
          api.GET('/repositories/{name}/maven/versions', {
            params: { path: { name: props.repo.name }, query: { group_id: coords.value.groupId, artifact_id: coords.value.artifactId, limit: 200 } },
          }),
        )
      : null,
  [ga],
)
const folder = computed(() => (coords.value && version.value ? `${coords.value.groupId.replaceAll('.', '/')}/${coords.value.artifactId}/${version.value}` : null))
const files = useAsync(
  async () => (folder.value ? call(api.GET('/repositories/{name}/browse', { params: { path: { name: props.repo.name }, query: { path: folder.value, limit: 500 } } })) : null),
  [folder],
)

const newest = computed(() => versions.data.value?.items.find((v) => !v.snapshot)?.version ?? versions.data.value?.items[0]?.version ?? null)

function dependency(v: string): string {
  const c = coords.value
  return c ? `<dependency><groupId>${c.groupId}</groupId><artifactId>${c.artifactId}</artifactId><version>${v}</version></dependency>` : ''
}

const crumbs = computed(() => [
  { label: t('browse.maven.artifacts') },
  ...(coords.value ? [{ label: `${coords.value.groupId}:${coords.value.artifactId}`, mono: true }] : []),
  ...(coords.value && version.value ? [{ label: version.value, mono: true }] : []),
])

function goTo(index: number): void {
  if (index === 0) {
    ga.value = null
    version.value = null
  } else version.value = null
}

const artifactColumns = computed<QTableColumn<MavenArtifact>[]>(() => [
  { name: 'artifact', label: t('browse.maven.artifact'), field: 'artifact_id', align: 'left' },
  { name: 'latest', label: t('browse.maven.latest'), field: (r) => r.latest ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'versions', label: t('browse.maven.versions'), field: 'version_count', align: 'right' },
  { name: 'updated', label: t('browse.modified'), field: (r) => r.updated_at ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
])

const versionColumns = computed<QTableColumn<MavenVersion>[]>(() => [
  { name: 'version', label: t('browse.maven.version'), field: 'version', align: 'left' },
  { name: 'files', label: t('browse.maven.files'), field: 'file_count', align: 'right' },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right' },
  { name: 'by', label: t('browse.maven.uploadedBy'), field: (r) => r.uploaded_by ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'updated', label: t('browse.modified'), field: (r) => r.updated_at ?? '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
])

const fileColumns = computed<QTableColumn<FolderEntry>[]>(() => [
  { name: 'name', label: t('browse.name'), field: 'name', align: 'left' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size ?? 0, align: 'right' },
  { name: 'modified', label: t('browse.modified'), field: (r) => r.last_modified ?? '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'actions', label: '', field: 'path', align: 'right' },
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

.pkg__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.pkg__install {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 320px;
}

.ver {
  font-weight: 600;
}

.snap {
  margin-left: 8px;
  height: 18px;
  font-size: 11px;
}

.entry {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}
</style>
