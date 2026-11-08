<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Dry run of a policy (UI_DESIGN §4.8 "Preview"): totals per repository and the candidate list, paged with
     "Load more" (next_cursor). Nothing is removed. Cards on phones. -->
<template>
  <q-dialog v-model="open" :maximized="$q.screen.lt.sm" transition-show="jump-up" transition-hide="jump-down">
    <q-card class="preview" :style="$q.screen.lt.sm ? undefined : { width: '1120px', maxWidth: '96vw' }">
      <header class="preview__head">
        <div class="col">
          <div class="preview__title">{{ title }}</div>
          <div class="preview__caption">{{ t('cleanup.preview.caption') }}</div>
        </div>
        <q-btn v-close-popup flat round dense :icon="iconClose" :aria-label="t('common.close')" />
      </header>

      <div v-if="first" class="preview__summary">
        <span class="preview__big">{{ t('cleanup.impact.count', { count: first.count, n: formatCount(first.count) }) }}</span>
        <span class="preview__big"><SizeCell :bytes="first.bytes" /></span>
        <span class="preview__repos">
          <span v-for="r in first.repositories.filter((x) => x.count > 0)" :key="r.name" class="preview__repo">
            <RepoChip :name="r.name" :format="r.format" :link="false" />
            {{ formatCount(r.count) }} · <SizeCell :bytes="r.bytes" />
          </span>
        </span>
      </div>
      <div v-if="first?.truncated" class="preview__warn"><q-icon :name="iconWarning" size="16px" />{{ t('cleanup.impact.truncated') }}</div>

      <div class="preview__body">
        <div v-if="error && !rows.length" class="preview__error" role="alert">
          {{ errorMessage(error) }}
          <q-btn flat dense no-caps :label="t('common.retry')" @click="load(true)" />
        </div>
        <q-table
          v-else
          class="ws-table"
          flat
          dense
          :rows="rows"
          :columns="columns"
          :row-key="(r: CleanupCandidate) => `${r.repository}:${r.kind}:${r.path}`"
          :loading="loading && !rows.length"
          :grid="$q.screen.lt.sm"
          :pagination="{ rowsPerPage: 0 }"
          hide-pagination
        >
          <template #body-cell-repository="{ row }">
            <q-td><RepoChip :name="row.repository" :format="formatOf(row.repository)" :link="false" /></q-td>
          </template>
          <template #body-cell-kind="{ row }">
            <q-td><span class="preview__kind">{{ t(`cleanup.preview.kind.${row.kind as CleanupCandidate['kind']}`) }}</span></q-td>
          </template>
          <template #body-cell-name="{ row }">
            <q-td class="mono preview__name">{{ row.name }}<q-tooltip>{{ row.path }}</q-tooltip></q-td>
          </template>
          <template #body-cell-version="{ row }">
            <q-td class="mono">{{ shortVersion(row.version) }}<q-tooltip v-if="row.version && row.version !== shortVersion(row.version)">{{ row.version }}</q-tooltip></q-td>
          </template>
          <template #body-cell-size="{ row }">
            <q-td class="text-right"><SizeCell :bytes="row.size" /></q-td>
          </template>
          <template #body-cell-lastDownloaded="{ row }">
            <q-td><TimeCell :value="row.last_downloaded_at" :placeholder="t('cleanup.preview.never')" /></q-td>
          </template>
          <template #body-cell-reason="{ row }">
            <q-td class="preview__reason">{{ row.reason }}</q-td>
          </template>

          <template #item="{ row }">
            <div class="preview__card">
              <div class="preview__card-head">
                <span class="mono preview__name">{{ row.name }}</span>
                <SizeCell :bytes="row.size" />
              </div>
              <div class="mono preview__card-version">{{ shortVersion(row.version) }}</div>
              <div class="preview__card-meta">
                <RepoChip :name="row.repository" :format="formatOf(row.repository)" :link="false" />
                <span class="preview__kind">{{ t(`cleanup.preview.kind.${row.kind as CleanupCandidate['kind']}`) }}</span>
              </div>
              <div class="preview__reason">{{ row.reason }}</div>
            </div>
          </template>

          <template #no-data>
            <EmptyState class="full-width" compact illustration="search" :title="t('cleanup.preview.empty')" :text="t('cleanup.preview.emptyText')" />
          </template>
        </q-table>
      </div>

      <footer class="preview__foot">
        <span v-if="first && rows.length" class="preview__showing">{{ t('cleanup.preview.showing', { shown: formatCount(rows.length), count: formatCount(first.count) }) }}</span>
        <q-space />
        <q-btn v-if="cursor" outline no-caps color="primary" :label="t('common.loadMore')" :loading="loading" @click="load(false)" />
        <q-btn v-close-popup flat no-caps :label="t('common.close')" />
      </footer>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed, ref, shallowRef, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { CleanupCandidate, CleanupPreview, Format } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import RepoChip from '@/components/RepoChip.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import type { PreviewScope } from '@/composables/useCleanupPreview'
import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconClose, iconWarning } from '@/utils/icons'

const props = defineProps<{ request: PreviewScope | null; title: string }>()
const open = defineModel<boolean>({ required: true })
const $q = useQuasar()

const PAGE = 50
const first = shallowRef<CleanupPreview | null>(null)
const rows = shallowRef<CleanupCandidate[]>([])
const cursor = ref<string | null>(null)
const loading = ref(false)
const error = ref<unknown>(null)
let generation = 0

const formats = computed(() => new Map((first.value?.repositories ?? []).map((r) => [r.name, r.format])))
const formatOf = (repo: string): Format | null => formats.value.get(repo) ?? null

const columns = computed<QTableColumn<CleanupCandidate>[]>(() => [
  { name: 'repository', label: t('cleanup.preview.col.repository'), field: 'repository', align: 'left' },
  { name: 'kind', label: t('cleanup.preview.col.kind'), field: 'kind', align: 'left' },
  { name: 'name', label: t('cleanup.preview.col.name'), field: 'name', align: 'left' },
  { name: 'version', label: t('cleanup.preview.col.version'), field: (r) => r.version ?? '', align: 'left' },
  { name: 'size', label: t('cleanup.preview.col.size'), field: 'size', align: 'right' },
  { name: 'lastDownloaded', label: t('cleanup.preview.col.lastDownloaded'), field: (r) => r.last_downloaded_at ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'reason', label: t('cleanup.preview.col.reason'), field: 'reason', align: 'left' },
])

/** Digests read better shortened: sha256:0123456789ab. */
function shortVersion(version: string | null): string {
  if (!version) return '–'
  return version.startsWith('sha256:') ? version.slice(0, 19) : version
}

async function load(reset: boolean): Promise<void> {
  if (!props.request) return
  const mine = reset ? ++generation : generation
  loading.value = true
  if (reset) error.value = null
  try {
    const page = await call(api.POST('/cleanup-policies/preview', { body: { ...props.request, limit: PAGE, cursor: reset ? null : cursor.value } }))
    if (mine !== generation) return
    if (reset) first.value = page
    rows.value = reset ? page.items : [...rows.value, ...page.items]
    cursor.value = page.next_cursor
  } catch (cause) {
    if (mine === generation) error.value = cause
  } finally {
    if (mine === generation) loading.value = false
  }
}

watch(open, (isOpen) => {
  if (!isOpen) return
  first.value = null
  rows.value = []
  cursor.value = null
  void load(true)
})
</script>

<style scoped>
.preview {
  display: flex;
  flex-direction: column;
  max-height: 92vh;
}

.preview__head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 18px 20px 12px;
}

.preview__title {
  font-size: var(--fs-16);
  font-weight: 650;
}

.preview__caption,
.preview__showing,
.preview__card-version {
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.preview__summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 20px;
  padding: 10px 20px;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  background: var(--surface-subtle);
}

.preview__big {
  font-size: var(--fs-20);
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}

.preview__repos {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.preview__repo {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.preview__warn {
  display: flex;
  gap: 6px;
  padding: 8px 20px;
  font-size: var(--fs-13);
  color: var(--status-degraded);
}

.preview__body {
  flex: 1;
  min-height: 160px;
  overflow-y: auto;
}

.preview__error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px 20px;
  color: var(--cache-error);
}

.preview__kind {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 6px;
  box-shadow: inset 0 0 0 1px var(--border);
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.preview__name {
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preview__reason {
  font-size: var(--fs-13);
  color: var(--text-secondary);
  white-space: normal;
  min-width: 160px;
}

.preview__card {
  width: 100%;
  display: grid;
  gap: 6px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.preview__card-head,
.preview__card-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.preview__foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
}
</style>
