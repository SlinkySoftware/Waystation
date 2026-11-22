<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Hugging Face browse: models and datasets → model page (download line, the revisions clients asked for and the
     commits they resolved to, cached commits with their revisions as chips) → commit page (its cached files by path:
     size, sha256, when cached, downloads). Hugging Face repositories are proxies only. Deep links: ?model=<name>
     (`datasets/<id>` for a dataset) and &commit=<40 hex>. -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="go" /></template>
    <template v-if="!model" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.huggingface.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <!-- Commit: its cached files -->
    <template v-if="model && commit">
      <div v-if="files.data.value" class="hf">
        <div class="hf__head">
          <div class="col">
            <div class="hf__name mono">{{ files.data.value.model }}</div>
            <div class="hf__meta">
              <span class="commit">
                {{ t('browse.huggingface.commit') }} <strong class="mono">{{ short(files.data.value.commit) }}</strong>
                <q-tooltip class="mono">{{ files.data.value.commit }}</q-tooltip>
                <q-btn flat dense round size="xs" :icon="iconCopy" :aria-label="t('browse.huggingface.copyCommit')" @click.stop="copy(files.data.value.commit, t('browse.huggingface.copyCommit'))" />
              </span>
              <span v-for="r in commitRevisions" :key="r" class="ws-badge tone-hit-outline chip mono">{{ r }}</span>
              <span>{{ t('browse.huggingface.fileCount', { count: allFiles.length }) }}<template v-if="filesCursor">+</template></span>
              <span><SizeCell :bytes="filesSize" /></span>
            </div>
          </div>
          <div class="hf__install">
            <div class="section-label">{{ t('browse.huggingface.download') }}</div>
            <CopyField :value="downloadLine(files.data.value.model, files.data.value.commit)" />
          </div>
        </div>
        <q-table class="ws-table" flat :rows="allFiles" :columns="fileColumns" row-key="path" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('browse.empty')">
          <template #body-cell-path="{ row }"><q-td class="mono path">{{ row.path }}</q-td></template>
          <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size" /></q-td></template>
          <template #body-cell-sha256="{ row }">
            <q-td class="digest">
              <span class="mono">{{ row.sha256.slice(0, 12) }}</span>
              <q-tooltip>sha256:{{ row.sha256 }}</q-tooltip>
              <q-btn flat dense round size="xs" :icon="iconCopy" :aria-label="t('browse.huggingface.copyChecksum')" @click.stop="copy(`sha256:${row.sha256}`, t('browse.huggingface.copyChecksum'))" />
            </q-td>
          </template>
          <template #body-cell-cached="{ row }"><q-td><TimeCell :value="row.cached_at ?? null" placeholder="" /></q-td></template>
          <template #body-cell-last="{ row }"><q-td><TimeCell :value="row.last_downloaded_at ?? null" placeholder="" /></q-td></template>
        </q-table>
        <div v-if="filesCursor" class="more">
          <q-btn flat no-caps color="primary" :loading="loadingMore" :label="t('browse.huggingface.loadMore')" @click="loadMore" />
        </div>
      </div>
      <div v-else-if="files.error.value" class="q-pa-md text-negative">{{ errorMessage(files.error.value) }}</div>
      <q-skeleton v-else height="300px" class="q-ma-md" />
    </template>

    <!-- Model: revisions and cached commits -->
    <template v-else-if="model">
      <div v-if="revisions.data.value" class="hf">
        <div class="hf__head">
          <div class="col">
            <div class="hf__name mono">
              {{ revisions.data.value.model.name }}
              <span v-if="revisions.data.value.model.kind === 'dataset'" class="ws-badge tone-offline badge">{{ t('browse.huggingface.dataset') }}</span>
            </div>
            <div class="hf__meta">
              <span v-if="revisions.data.value.model.latest_commit">
                {{ t('browse.huggingface.mainAt') }} <strong class="mono">{{ short(revisions.data.value.model.latest_commit) }}</strong>
              </span>
              <span>{{ t('browse.huggingface.revisionCount', { count: revisions.data.value.model.revisions }) }}</span>
              <span>{{ t('browse.huggingface.fileCount', { count: revisions.data.value.model.files }) }}</span>
              <span><SizeCell :bytes="revisions.data.value.model.size_bytes" /></span>
            </div>
          </div>
          <div class="hf__install">
            <div class="section-label">{{ t('browse.huggingface.download') }}</div>
            <CopyField :value="downloadLine(revisions.data.value.model.name)" />
          </div>
        </div>

        <div class="section-label hf__section">{{ t('browse.huggingface.commits') }}</div>
        <q-table
          class="ws-table"
          flat
          :rows="revisions.data.value.commits"
          :columns="commitColumns"
          row-key="commit"
          :pagination="{ rowsPerPage: 0 }"
          hide-pagination
          :no-data-label="t('browse.huggingface.noCommits')"
          @row-click="(_e: Event, row: HuggingfaceCommit) => (commit = row.commit)"
        >
          <template #body-cell-commit="{ row }">
            <q-td class="cursor-pointer">
              <span class="mono commit-id">{{ short(row.commit) }}</span>
              <q-tooltip class="mono">{{ row.commit }}</q-tooltip>
              <q-btn flat dense round size="xs" :icon="iconCopy" :aria-label="t('browse.huggingface.copyCommit')" @click.stop="copy(row.commit, t('browse.huggingface.copyCommit'))" />
              <span v-for="r in row.revisions" :key="r" class="ws-badge tone-hit-outline chip mono">{{ r }}</span>
            </q-td>
          </template>
          <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td></template>
          <template #body-cell-cached="{ row }"><q-td><TimeCell :value="row.cached_at ?? null" placeholder="" /></q-td></template>
          <template #body-cell-last="{ row }"><q-td><TimeCell :value="row.last_downloaded_at ?? null" placeholder="" /></q-td></template>
        </q-table>

        <div class="section-label hf__section">{{ t('browse.huggingface.revisions') }}</div>
        <q-table
          class="ws-table"
          flat
          :rows="revisions.data.value.refs"
          :columns="refColumns"
          row-key="revision"
          :pagination="{ rowsPerPage: 0 }"
          hide-pagination
          :no-data-label="t('browse.huggingface.noRevisions')"
          @row-click="(_e: Event, row: HuggingfaceRef) => openCommit(row.commit)"
        >
          <template #body-cell-revision="{ row }"><q-td class="cursor-pointer mono rev">{{ row.revision }}</q-td></template>
          <template #body-cell-commit="{ row }">
            <q-td class="cursor-pointer">
              <span class="mono">{{ short(row.commit) }}</span>
              <q-tooltip class="mono">{{ row.commit }}</q-tooltip>
              <q-btn flat dense round size="xs" :icon="iconCopy" :aria-label="t('browse.huggingface.copyCommit')" @click.stop="copy(row.commit, t('browse.huggingface.copyCommit'))" />
            </q-td>
          </template>
          <template #body-cell-resolved="{ row }"><q-td><TimeCell :value="row.resolved_at ?? null" placeholder="" /></q-td></template>
        </q-table>
      </div>
      <div v-else-if="revisions.error.value" class="q-pa-md text-negative">{{ errorMessage(revisions.error.value) }}</div>
      <q-skeleton v-else height="300px" class="q-ma-md" />
    </template>

    <!-- Models and datasets -->
    <EmptyState
      v-else-if="models.data.value && !models.data.value.items.length && !search"
      compact
      illustration="cache"
      :title="t('browse.huggingface.emptyTitle')"
      :text="t('browse.huggingface.emptyText')"
    />
    <q-table
      v-else
      class="ws-table"
      flat
      :rows="models.data.value?.items ?? []"
      :columns="modelColumns"
      row-key="name"
      :loading="models.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: HuggingfaceModel) => open(row.name)"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <span class="mono model-name">{{ row.name }}</span>
          <span v-if="row.kind === 'dataset'" class="ws-badge tone-offline badge">{{ t('browse.huggingface.dataset') }}</span>
        </q-td>
      </template>
      <template #body-cell-latest="{ row }">
        <q-td class="mono gt-xs">
          <template v-if="row.latest_commit">{{ short(row.latest_commit) }}<q-tooltip class="mono">{{ row.latest_commit }}</q-tooltip></template>
        </q-td>
      </template>
      <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td></template>
      <template #body-cell-last="{ row }"><q-td class="gt-sm"><TimeCell :value="row.last_downloaded_at ?? null" placeholder="" /></q-td></template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { HuggingfaceCommit, HuggingfaceFile, HuggingfaceModel, HuggingfaceRef, Repository } from '@/api/types'
import CopyField from '@/components/CopyField.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy, useNotify } from '@/composables/useNotify'
import { useQueryNavigate, useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconCopy, iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const PAGE = 500
const copy = useCopy()
const notify = useNotify()
const model = useQueryParam('model')
const commit = useQueryParam('commit')
const nav = useQueryNavigate()
const search = ref<string | null>(null)

const models = useAsync(
  async () =>
    model.value ? null : call(api.GET('/repositories/{name}/huggingface/models', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [model, search],
)
// The model page; the commit page uses it too for the revisions that resolve to the commit.
const revisions = useAsync(
  async () => (model.value ? call(api.GET('/repositories/{name}/huggingface/revisions', { params: { path: { name: props.repo.name }, query: { model: model.value } } })) : null),
  [model],
)
const files = useAsync(
  async () =>
    model.value && commit.value
      ? call(api.GET('/repositories/{name}/huggingface/files', { params: { path: { name: props.repo.name }, query: { model: model.value, commit: commit.value, limit: PAGE } } }))
      : null,
  [model, commit],
)

// "Load more" pages of the commit's files, appended to the first page.
const extraFiles = ref<HuggingfaceFile[]>([])
const extraCursor = ref<string | null | undefined>(undefined)
const loadingMore = ref(false)
watch([model, commit], () => {
  extraFiles.value = []
  extraCursor.value = undefined
})
const allFiles = computed(() => [...(files.data.value?.items ?? []), ...extraFiles.value])
const filesCursor = computed(() => (extraCursor.value === undefined ? (files.data.value?.next_cursor ?? null) : extraCursor.value))
const filesSize = computed(() => allFiles.value.reduce((sum, f) => sum + f.size, 0))

async function loadMore(): Promise<void> {
  if (!model.value || !commit.value || !filesCursor.value) return
  loadingMore.value = true
  try {
    const page = await call(
      api.GET('/repositories/{name}/huggingface/files', {
        params: { path: { name: props.repo.name }, query: { model: model.value, commit: commit.value, cursor: filesCursor.value, limit: PAGE } },
      }),
    )
    extraFiles.value = [...extraFiles.value, ...page.items]
    extraCursor.value = page.next_cursor
  } catch (e) {
    notify.error(e)
  } finally {
    loadingMore.value = false
  }
}

const commitRevisions = computed(() => revisions.data.value?.commits.find((c) => c.commit === commit.value)?.revisions ?? [])

function short(sha: string): string {
  return sha.slice(0, 12)
}

function open(name: string): void {
  search.value = null
  nav({ model: name, commit: null })
}

function openCommit(sha: string): void {
  commit.value = sha
}

function go(index: number): void {
  if (index === 0) nav({ model: null, commit: null })
  else nav({ commit: null })
}

/** The hf CLI line for a model or dataset (`datasets/<id>`), at a commit when given. */
function downloadLine(name: string, sha?: string): string {
  const dataset = name.startsWith('datasets/')
  const id = dataset ? name.slice('datasets/'.length) : name
  const endpoint = `${window.location.origin}/repository/${props.repo.name}`
  return `HF_ENDPOINT=${endpoint} hf download ${id}${dataset ? ' --repo-type dataset' : ''}${sha ? ` --revision ${sha}` : ''}`
}

const crumbs = computed(() => [
  { label: t('browse.huggingface.models') },
  ...(model.value ? [{ label: model.value, mono: true }] : []),
  ...(model.value && commit.value ? [{ label: short(commit.value), mono: true }] : []),
])

const modelColumns = computed<QTableColumn<HuggingfaceModel>[]>(() => [
  { name: 'name', label: t('browse.huggingface.model'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.huggingface.mainAt'), field: (r) => r.latest_commit ?? '', align: 'left', headerClasses: 'gt-xs' },
  { name: 'revisions', label: t('browse.huggingface.revisions'), field: 'revisions', align: 'right', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'files', label: t('browse.huggingface.files'), field: 'files', align: 'right', sortable: true },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right', sortable: true },
  { name: 'downloads', label: t('browse.downloads'), field: 'downloads', format: (v: number) => formatCount(v), align: 'right', sortable: true, classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'last', label: t('browse.lastDownloaded'), field: (r) => r.last_downloaded_at ?? '', align: 'left', sortable: true, headerClasses: 'gt-sm' },
])

const commitColumns = computed<QTableColumn<HuggingfaceCommit>[]>(() => [
  { name: 'commit', label: t('browse.huggingface.commit'), field: 'commit', align: 'left' },
  { name: 'files', label: t('browse.huggingface.files'), field: 'files', align: 'right' },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right' },
  { name: 'cached', label: t('browse.huggingface.cached'), field: (r) => r.cached_at ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'downloads', label: t('browse.downloads'), field: 'downloads', format: (v: number) => formatCount(v), align: 'right', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'last', label: t('browse.lastDownloaded'), field: (r) => r.last_downloaded_at ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
])

const refColumns = computed<QTableColumn<HuggingfaceRef>[]>(() => [
  { name: 'revision', label: t('browse.huggingface.revision'), field: 'revision', align: 'left' },
  { name: 'commit', label: t('browse.huggingface.commit'), field: 'commit', align: 'left' },
  { name: 'resolved', label: t('browse.huggingface.resolved'), field: (r) => r.resolved_at ?? '', align: 'left' },
])

const fileColumns = computed<QTableColumn<HuggingfaceFile>[]>(() => [
  { name: 'path', label: t('browse.huggingface.path'), field: 'path', align: 'left', sortable: true },
  { name: 'size', label: t('browse.size'), field: 'size', align: 'right', sortable: true },
  { name: 'sha256', label: t('browse.huggingface.checksum'), field: 'sha256', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'cached', label: t('browse.huggingface.cached'), field: (r) => r.cached_at ?? '', align: 'left', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'downloads', label: t('browse.downloads'), field: 'downloads', format: (v: number) => formatCount(v), align: 'right', sortable: true, classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'last', label: t('browse.lastDownloaded'), field: (r) => r.last_downloaded_at ?? '', align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
])
</script>

<style scoped>
.browse-search {
  width: 260px;
}

.model-name {
  font-weight: 600;
  overflow-wrap: anywhere;
}

.hf__head {
  display: flex;
  flex-wrap: wrap;
  gap: 16px 24px;
  padding: 16px;
  border-top: 1px solid var(--border);
}

.hf__name {
  font-size: var(--fs-16);
  font-weight: 650;
  overflow-wrap: anywhere;
}

.hf__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 16px;
  margin-top: 6px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.hf__install {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 280px;
  max-width: 100%;
}

.hf__section {
  padding: 16px 16px 6px;
  border-top: 1px solid var(--border);
}

.commit-id,
.rev {
  font-weight: 600;
}

.path {
  overflow-wrap: anywhere;
  white-space: normal;
}

.badge {
  margin-left: 8px;
  height: 18px;
  font-size: 11px;
}

.chip {
  margin-left: 6px;
  height: 18px;
  font-size: 11px;
}

.hf__meta .chip {
  margin-left: 0;
}

.digest {
  color: var(--text-secondary);
  font-size: var(--fs-12);
  white-space: nowrap;
}

.more {
  display: flex;
  justify-content: center;
  padding: 8px;
  border-top: 1px solid var(--border);
}
</style>
