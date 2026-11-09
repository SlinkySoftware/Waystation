<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Job runs (UI_DESIGN §4.9): filters in the URL (status, job, task, routine), newest first with "Load more", live
     progress bars and status changes from the `tasks` topic; a new run pulls in the first page again. Row click opens
     the run drawer (log viewer). Cards on phones. -->
<template>
  <PageCard :title="t('tasks.runs.title')" :caption="t('tasks.runs.caption')" flush>
    <template #actions>
      <q-btn flat round dense :icon="iconRefresh" :aria-label="t('tasks.refresh')" :loading="loading" @click="reload">
        <q-tooltip>{{ t('tasks.refresh') }}</q-tooltip>
      </q-btn>
    </template>
    <div class="runs__filters">
      <q-select v-model="statusFilter" multiple outlined dense emit-value map-options options-dense :options="statusOptions" :label="t('tasks.runs.filter.status')" class="runs__filter" clearable />
      <q-select v-model="kindFilter" outlined dense emit-value map-options options-dense :options="kindOptions" :label="t('tasks.runs.filter.kind')" class="runs__filter" clearable />
      <q-chip v-if="taskFilter" removable dense outline color="primary" :label="t('tasks.runs.filter.task', { task: taskFilter })" @remove="taskFilter = null" />
      <q-chip v-if="jobFilter" removable dense outline color="primary" class="mono" :label="t('tasks.runs.filter.job', { job: jobFilter })" @remove="jobFilter = null" />
      <q-toggle v-model="routine" dense :label="t('tasks.runs.filter.routine')" />
    </div>
    <q-table
      class="ws-table runs"
      flat
      :rows="rows"
      :columns="columns"
      row-key="id"
      :loading="loading && !rows.length"
      :grid="$q.screen.lt.sm"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      @row-click="(_e: Event, row: JobRunSummary) => emit('open', row.id)"
    >
      <template #body-cell-status="{ row }">
        <q-td class="cursor-pointer"><RunStatusPill :status="row.status" /></q-td>
      </template>
      <template #body-cell-task="{ row }">
        <q-td class="cursor-pointer">
          <div class="runs__name">{{ kindLabel(row.kind) }}</div>
          <div class="runs__sub mono">{{ row.task_id ?? row.job_id }}</div>
        </q-td>
      </template>
      <template #body-cell-trigger="{ row }">
        <q-td>
          <span class="ws-badge runs__trigger">{{ triggerLabel(row.trigger) }}</span>
          <span v-if="row.attempt > 1" class="runs__sub"> {{ t('tasks.runs.attempt', { n: row.attempt }) }}</span>
        </q-td>
      </template>
      <template #body-cell-started="{ row }">
        <q-td><TimeCell :value="row.started_at" /></q-td>
      </template>
      <template #body-cell-duration="{ row }">
        <q-td class="num">{{ formatDuration(runSeconds(row, now)) }}</q-td>
      </template>
      <template #body-cell-progress="{ row }">
        <q-td class="runs__progress"><RunDetail :run="row" /></q-td>
      </template>
      <template #body-cell-worker="{ row }">
        <q-td class="runs__sub mono">{{ row.worker_id ?? '–' }}</q-td>
      </template>

      <template #item="{ row }">
        <div class="runs__card cursor-pointer" role="button" tabindex="0" @click="emit('open', row.id)" @keydown.enter="emit('open', row.id)">
          <div class="runs__card-head">
            <RunStatusPill :status="row.status" :tooltip="false" />
            <span class="runs__name">{{ kindLabel(row.kind) }}</span>
            <q-space />
            <TimeCell :value="row.started_at" />
          </div>
          <RunDetail :run="row" />
        </div>
      </template>

      <template #no-data>
        <EmptyState class="full-width" compact illustration="activity" :title="filtered ? t('tasks.runs.emptyFiltered') : t('tasks.runs.emptyTitle')" :text="filtered ? undefined : t('tasks.runs.emptyText')">
          <q-btn v-if="filtered" flat no-caps color="primary" :label="t('tasks.runs.filter.clear')" @click="clearFilters" />
        </EmptyState>
      </template>
    </q-table>
    <template v-if="next" #footer>
      <q-btn flat no-caps color="primary" class="full-width" :label="t('tasks.runs.loadMore')" :loading="loadingMore" @click="loadMore" />
    </template>
  </PageCard>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed, onScopeDispose, ref, watch } from 'vue'

import { api, call } from '@/api/client'
import type { JobRunSummary, RunStatus } from '@/api/types'
import { runSeconds } from '@/components/cleanup/cleanup'
import RunDetail from '@/components/cleanup/RunDetail.vue'
import RunStatusPill from '@/components/cleanup/RunStatusPill.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useNotify } from '@/composables/useNotify'
import { useNow } from '@/composables/useNow'
import { useQueryNavigate, useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { useLiveStore } from '@/stores/live'
import { formatDuration } from '@/utils/format'
import { iconRefresh } from '@/utils/icons'

import { applyRunEvent, kindLabel, triggerLabel } from './tasks'

const emit = defineEmits<{ open: [runId: string] }>()
const $q = useQuasar()
const notify = useNotify()
const now = useNow()
const live = useLiveStore()
const navigate = useQueryNavigate()

const STATUSES: RunStatus[] = ['running', 'succeeded', 'failed', 'dead', 'interrupted']
const statusParam = useQueryParam('status')
const kindFilter = useQueryParam('kind')
const taskFilter = useQueryParam('task')
const jobFilter = useQueryParam('job')
const routineParam = useQueryParam('routine')
const statusFilter = computed<RunStatus[]>({
  get: () => (statusParam.value ?? '').split(',').filter((s): s is RunStatus => (STATUSES as string[]).includes(s)),
  set: (value) => (statusParam.value = value?.length ? value.join(',') : null),
})
const routine = computed({ get: () => routineParam.value === '1', set: (on: boolean) => (routineParam.value = on ? '1' : null) })
const filtered = computed(() => Boolean(statusFilter.value.length || kindFilter.value || taskFilter.value || jobFilter.value))

const statusOptions = computed(() => STATUSES.map((value) => ({ value, label: t(`cleanup.run.${value}`) })))
const kindOptions = computed(() => ['blob.gc', 'cleanup.run', 'stats.rollup'].map((value) => ({ value, label: kindLabel(value) })))

const rows = ref<JobRunSummary[]>([])
const next = ref<string | null>(null)
const loading = ref(false)
const loadingMore = ref(false)
let generation = 0

function query(cursor?: string) {
  return {
    status: statusFilter.value.length ? statusFilter.value : undefined,
    kind: kindFilter.value ?? undefined,
    task: taskFilter.value ?? undefined,
    job: jobFilter.value ?? undefined,
    include_routine: routine.value || undefined,
    cursor,
    limit: 50,
  }
}

async function reload(): Promise<void> {
  const mine = ++generation
  loading.value = true
  try {
    const page = await call(api.GET('/tasks/runs', { params: { query: query() } }))
    if (mine !== generation) return
    rows.value = page.items
    next.value = page.next_cursor
  } catch (error) {
    if (mine === generation) notify.error(error)
  } finally {
    if (mine === generation) loading.value = false
  }
}

/** A new run appeared live: merge the first page in front of what is loaded (keeps "Load more" pages). */
async function refreshHead(): Promise<void> {
  const mine = generation
  try {
    const page = await call(api.GET('/tasks/runs', { params: { query: query() } }))
    if (mine !== generation) return
    const fresh = new Map(page.items.map((r) => [r.id, r]))
    rows.value = [...page.items, ...rows.value.filter((r) => !fresh.has(r.id))]
  } catch {
    // the next event or a manual refresh tries again
  }
}

async function loadMore(): Promise<void> {
  if (!next.value) return
  loadingMore.value = true
  try {
    const page = await call(api.GET('/tasks/runs', { params: { query: query(next.value) } }))
    const known = new Set(rows.value.map((r) => r.id))
    rows.value = [...rows.value, ...page.items.filter((r) => !known.has(r.id))]
    next.value = page.next_cursor
  } catch (error) {
    notify.error(error)
  } finally {
    loadingMore.value = false
  }
}

function clearFilters(): void {
  navigate({ status: null, kind: null, task: null, job: null })
}

watch(() => [statusParam.value, kindFilter.value, taskFilter.value, jobFilter.value, routine.value], () => void reload(), { immediate: true })

let headTimer: ReturnType<typeof setTimeout> | null = null
const off = live.onTasks((event) => {
  if (event.type !== 'job.update' && event.type !== 'job.progress') return
  if (applyRunEvent(rows.value, event) || event.type !== 'job.update') return
  headTimer ??= setTimeout(() => {
    headTimer = null
    void refreshHead()
  }, 400)
})
onScopeDispose(() => {
  off()
  if (headTimer) clearTimeout(headTimer)
})

defineExpose({ reload })

const columns = computed<QTableColumn<JobRunSummary>[]>(() => [
  { name: 'status', label: t('tasks.runs.col.status'), field: 'status', align: 'left' },
  { name: 'task', label: t('tasks.runs.col.task'), field: 'kind', align: 'left' },
  { name: 'trigger', label: t('tasks.runs.col.trigger'), field: 'trigger', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'started', label: t('tasks.runs.col.started'), field: 'started_at', align: 'left' },
  { name: 'duration', label: t('tasks.runs.col.duration'), field: (r) => r.duration_ms ?? 0, align: 'left' },
  { name: 'progress', label: t('tasks.runs.col.progress'), field: (r) => r.progress.done, align: 'left' },
  { name: 'worker', label: t('tasks.runs.col.worker'), field: (r) => r.worker_id ?? '', align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
])
</script>

<style scoped>
.runs__filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 0 16px 12px;
}

.runs__filter {
  min-width: 180px;
}

.runs__name {
  font-weight: 600;
}

.runs__sub {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.runs__trigger {
  color: var(--text-secondary);
  box-shadow: inset 0 0 0 1px var(--border);
}

.runs__progress {
  min-width: 180px;
  white-space: normal;
}

.runs__card {
  width: 100%;
  display: grid;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.runs__card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
