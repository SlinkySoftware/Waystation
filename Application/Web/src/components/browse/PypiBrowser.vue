<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- PyPI browse: projects → project page (install snippet, versions newest first with their wheels and sdists, cache
     state; hosted: uploader and yank / un-yank). Groups are browsed through their members. -->
<template>
  <PageCard v-if="repo.type === 'group'" flush>
    <template #title><BrowseCrumbs :crumbs="[{ label: t('browse.pypi.projects') }]" /></template>
    <EmptyState compact illustration="repositories" :title="t('browse.pypi.groupTitle')" :text="t('browse.pypi.groupText')">
      <div class="members">
        <router-link v-for="m in repo.group?.members ?? []" :key="m" :to="{ name: 'repository', params: { name: m, tab: 'browse' } }" class="member mono">{{ m }}</router-link>
      </div>
    </EmptyState>
  </PageCard>

  <PageCard v-else flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="project = null" /></template>
    <template v-if="!project" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.pypi.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="project">
      <div v-if="versions.data.value" class="pkg">
        <div class="pkg__head">
          <div class="col">
            <div class="pkg__name mono">{{ versions.data.value.project.display_name }}</div>
            <div v-if="versions.data.value.project.summary" class="pkg__desc">{{ versions.data.value.project.summary }}</div>
            <div class="pkg__meta">
              <span v-if="versions.data.value.project.latest">{{ t('browse.pypi.latest') }} <strong class="mono">{{ versions.data.value.project.latest }}</strong></span>
              <span>{{ t('browse.pypi.versionCount', { count: versions.data.value.project.version_count }) }}</span>
              <span>{{ t(hosted ? 'browse.pypi.filesStored' : 'browse.pypi.filesCached', { count: versions.data.value.project.files }) }}</span>
              <span><SizeCell :bytes="versions.data.value.project.size_bytes" /></span>
            </div>
          </div>
          <div v-if="versions.data.value.project.latest" class="pkg__install">
            <div class="section-label">{{ t('browse.pypi.install') }}</div>
            <CopyField :value="`pip install ${versions.data.value.project.display_name}==${versions.data.value.project.latest}`" />
          </div>
        </div>

        <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" row-key="version" :pagination="{ rowsPerPage: 0 }" hide-pagination>
          <template #body="scope">
            <q-tr :props="scope" class="cursor-pointer" @click="toggle(scope.row.version)">
              <q-td key="version" :props="scope">
                <q-icon :name="iconChevronRight" size="16px" class="chev" :class="{ 'chev--open': expanded.has(scope.row.version) }" />
                <span class="mono ver">{{ scope.row.version }}</span>
                <span v-if="scope.row.prerelease" class="ws-badge tone-offline badge">{{ t('browse.pypi.prerelease') }}</span>
                <span v-if="scope.row.yanked" class="ws-badge tone-degraded badge">
                  {{ t('browse.pypi.yanked') }}
                  <q-tooltip v-if="yankReason(scope.row)" max-width="360px">{{ yankReason(scope.row) }}</q-tooltip>
                </span>
              </q-td>
              <q-td key="files" :props="scope">{{ t('browse.pypi.filesOf', { cached: scope.row.cached_files, total: scope.row.files.length }) }}</q-td>
              <q-td key="python" :props="scope" class="mono">{{ scope.row.files.find((f: PypiFile) => f.requires_python)?.requires_python ?? '' }}</q-td>
              <q-td key="size" :props="scope" class="text-right"><SizeCell :bytes="scope.row.size_bytes" /></q-td>
              <q-td key="actions" :props="scope" auto-width>
                <q-btn
                  v-if="hosted"
                  flat
                  dense
                  no-caps
                  size="sm"
                  :label="scope.row.yanked ? t('browse.pypi.unyank') : t('browse.pypi.yank')"
                  @click.stop="scope.row.yanked ? setYanked(scope.row.version, false, null) : openYank(scope.row.version)"
                />
              </q-td>
            </q-tr>
            <q-tr v-if="expanded.has(scope.row.version)" :props="scope" class="files-row">
              <q-td colspan="100%">
                <div v-if="!scope.row.files.length" class="text-faint">{{ t('browse.pypi.noFiles') }}</div>
                <div v-for="f in scope.row.files" :key="f.filename" class="file">
                  <span class="ws-badge tone-offline kind">{{ kindLabel(f) }}</span>
                  <span class="mono file__name" :title="f.filename">{{ f.filename }}</span>
                  <span class="file__size"><SizeCell :bytes="f.size ?? 0" /></span>
                  <span class="file__cache">
                    <CacheBadge v-if="f.cached" state="HIT" />
                    <span v-else class="text-faint">{{ t('browse.notCached') }}</span>
                  </span>
                  <span v-if="f.core_metadata" class="ws-badge tone-offline">{{ t('browse.pypi.metadata') }}<q-tooltip>{{ t('browse.pypi.metadataHelp') }}</q-tooltip></span>
                  <span v-if="f.uploaded_by" class="file__by">{{ t('browse.pypi.uploadedBy', { user: f.uploaded_by }) }} <TimeCell :value="f.uploaded_at" /></span>
                  <span v-if="f.sha256" class="file__sha mono" :title="f.sha256">sha256:{{ f.sha256.slice(0, 12) }}</span>
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
      :rows="projects.data.value?.items ?? []"
      :columns="projectColumns"
      row-key="name"
      :loading="projects.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: PypiProject) => (project = row.name)"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <div class="mono pkg-name">{{ row.display_name }}</div>
          <div class="pkg-desc">{{ row.summary }}</div>
        </q-td>
      </template>
      <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td></template>
      <template #body-cell-last="{ row }"><q-td><TimeCell :value="row.last_downloaded_at" :placeholder="t('repos.never')" /></q-td></template>
    </q-table>

    <FormDialog v-model="yankOpen" :title="t('browse.pypi.yankTitle', { version: yankVersion ?? '' })" :caption="t('browse.pypi.yankCaption')" :saving="yanking" :submit-label="t('browse.pypi.yank')" width="460px" @submit="setYanked(yankVersion ?? '', true, yankReasonText)">
      <q-input v-model="yankReasonText" outlined dense autofocus maxlength="500" :label="t('browse.pypi.yankReason')" />
    </FormDialog>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { PypiFile, PypiProject, PypiVersion, Repository } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import EmptyState from '@/components/EmptyState.vue'
import FormDialog from '@/components/FormDialog.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconChevronRight, iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const notify = useNotify()
const project = useQueryParam('project')
const search = ref<string | null>(null)
const expanded = ref(new Set<string>())
const hosted = computed(() => props.repo.type === 'hosted')

const projects = useAsync(
  async () =>
    props.repo.type === 'group' ? null : call(api.GET('/repositories/{name}/pypi/projects', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const versions = useAsync(
  async () => (project.value ? call(api.GET('/repositories/{name}/pypi/versions', { params: { path: { name: props.repo.name }, query: { project: project.value, limit: 100 } } })) : null),
  [project],
)

watch(project, () => {
  expanded.value = new Set()
})
watch(
  () => versions.data.value,
  (page) => {
    // open the newest version so its files show without a click
    if (page?.items.length && !expanded.value.size) expanded.value = new Set([page.items[0]!.version])
  },
)

const crumbs = computed(() => [{ label: t('browse.pypi.projects') }, ...(project.value ? [{ label: versions.data.value?.project.display_name ?? project.value, mono: true }] : [])])

function toggle(version: string): void {
  const next = new Set(expanded.value)
  if (next.has(version)) next.delete(version)
  else next.add(version)
  expanded.value = next
}

function kindLabel(file: PypiFile): string {
  return t(`browse.pypi.kind.${file.kind}`)
}

function yankReason(row: PypiVersion): string | null {
  return row.files.find((f) => f.yanked_reason)?.yanked_reason ?? null
}

// ---- yank (hosted): PEP 592, every file of the version
const yankOpen = ref(false)
const yankVersion = ref<string | null>(null)
const yankReasonText = ref('')
const yanking = ref(false)

function openYank(version: string): void {
  yankVersion.value = version
  yankReasonText.value = ''
  yankOpen.value = true
}

async function setYanked(version: string, yanked: boolean, reason: string | null): Promise<void> {
  if (!project.value) return
  yanking.value = true
  try {
    await call(api.POST('/repositories/{name}/pypi/yank', { params: { path: { name: props.repo.name } }, body: { project: project.value, version, yanked, reason: reason?.trim() || null } }))
    notify.success(yanked ? t('browse.pypi.yankedDone', { version }) : t('browse.pypi.unyankedDone', { version }))
    yankOpen.value = false
    versions.reload()
  } catch (error) {
    notify.error(error)
  } finally {
    yanking.value = false
  }
}

const projectColumns = computed<QTableColumn<PypiProject>[]>(() => [
  { name: 'name', label: t('browse.pypi.project'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.pypi.latest'), field: (r) => r.latest ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'files', label: hosted.value ? t('browse.pypi.stored') : t('browse.pypi.cached'), field: (r) => `${r.files} / ${r.version_count}`, align: 'right', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right', sortable: true },
  { name: 'downloads', label: t('browse.downloads'), field: (r) => r.downloads ?? 0, format: (v: number) => formatCount(v), align: 'right', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'last', label: t('browse.lastDownloaded'), field: (r) => r.last_downloaded_at ?? '', align: 'left', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
])

const columns = computed<QTableColumn<PypiVersion>[]>(() => [
  { name: 'version', label: t('browse.pypi.version'), field: 'version', align: 'left' },
  { name: 'files', label: t('browse.pypi.files'), field: (r) => r.files.length, align: 'left' },
  { name: 'python', label: t('browse.pypi.requiresPython'), field: () => '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right' },
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

.file__name {
  flex: 1 1 280px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file__size {
  width: 80px;
  text-align: right;
}

.file__by,
.file__sha {
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
