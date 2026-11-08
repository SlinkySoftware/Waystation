<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Blob garbage collection (UI_DESIGN §4.7 GC status): last run, bytes reclaimed, what waits out the grace period,
     schedule and next run, "Run now", and recent runs. Refreshes every 2 s while a run is in progress. -->
<template>
  <PageCard :title="t('cleanup.gc.title')" :caption="status ? t('cleanup.gc.caption', { hours: status.grace_hours }) : undefined">
    <template #actions>
      <q-btn v-if="canRun" outline no-caps color="primary" :icon="iconPlay" :label="t('cleanup.gc.run')" :loading="starting" :disable="!status || running" @click="runNow" />
    </template>

    <div v-if="gc.error.value && !status" class="gc__error" role="alert">
      {{ forbidden ? t('cleanup.gc.forbidden') : errorMessage(gc.error.value) }}
      <q-btn v-if="!forbidden" flat dense no-caps :label="t('common.retry')" @click="gc.reload" />
    </div>
    <q-skeleton v-else-if="!status" height="96px" />
    <template v-else>
      <dl class="gc__stats">
        <div class="gc__stat">
          <dt>{{ t('cleanup.gc.lastRun') }}</dt>
          <dd v-if="latest">
            <RunStatusPill :status="latest.status" />
            <TimeCell :value="latest.started_at" />
            <RunDetail v-if="latest.status !== 'succeeded'" :run="latest" class="gc__detail" />
          </dd>
          <dd v-else class="text-faint">{{ t('cleanup.gc.none') }}</dd>
        </div>
        <div class="gc__stat">
          <dt>{{ t('cleanup.gc.reclaimed') }}</dt>
          <dd>
            <span class="gc__value"><SizeCell :bytes="lastDone?.freed_bytes ?? null" /></span>
            <span v-if="lastDone" class="gc__sub">{{ t('cleanup.gc.blobs', { count: lastDone.freed_blobs, n: formatCount(lastDone.freed_blobs) }) }}</span>
          </dd>
        </div>
        <div class="gc__stat">
          <dt>{{ t('cleanup.gc.pending') }}</dt>
          <dd>
            <span class="gc__value"><SizeCell :bytes="status.pending_bytes" /></span>
            <span class="gc__sub">{{ t('cleanup.gc.pendingLine', { count: status.pending_blobs, n: formatCount(status.pending_blobs) }) }}</span>
          </dd>
        </div>
        <div class="gc__stat">
          <dt>{{ t('cleanup.gc.grace') }}</dt>
          <dd><span class="gc__value">{{ t('cleanup.gc.graceValue', { hours: status.grace_hours }) }}</span></dd>
        </div>
        <div class="gc__stat">
          <dt>{{ t('cleanup.gc.nextRun') }}</dt>
          <dd>
            <span class="gc__value"><TimeCell :value="status.next_run_at" :placeholder="t('cleanup.gc.disabled')" /></span>
            <span class="gc__sub">{{ describeSchedule(status.schedule) }}</span>
          </dd>
        </div>
      </dl>

      <div class="gc__recent-title">{{ t('cleanup.gc.recent') }}</div>
      <q-table class="ws-table gc__table" flat dense :rows="status.runs.slice(0, 8)" :columns="columns" row-key="id" :grid="$q.screen.lt.sm" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('cleanup.gc.none')">
        <template #body-cell-status="{ row }">
          <q-td><RunStatusPill :status="row.status" /></q-td>
        </template>
        <template #body-cell-started="{ row }">
          <q-td><TimeCell :value="row.started_at" /></q-td>
        </template>
        <template #body-cell-duration="{ row }">
          <q-td class="num">{{ formatDuration(runSeconds(row, now)) }}</q-td>
        </template>
        <template #body-cell-freed="{ row }">
          <q-td class="text-right"><SizeCell :bytes="row.freed_bytes" /> <span class="gc__sub">· {{ formatCount(row.freed_blobs) }}</span></q-td>
        </template>
        <template #item="{ row }">
          <div class="gc__card">
            <RunStatusPill :status="row.status" :tooltip="false" />
            <TimeCell :value="row.started_at" />
            <span class="gc__sub"><SizeCell :bytes="row.freed_bytes" /> · {{ formatDuration(runSeconds(row, now)) }}</span>
            <RunDetail v-if="row.status !== 'succeeded'" :run="row" class="full-width" />
          </div>
        </template>
      </q-table>
    </template>
  </PageCard>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed, ref, watch } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { GcRun } from '@/api/types'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { useNow } from '@/composables/useNow'
import { usePolling } from '@/composables/usePolling'
import { t } from '@/i18n'
import { formatCount, formatDuration } from '@/utils/format'
import { iconPlay } from '@/utils/icons'

import { describeSchedule, runSeconds } from './cleanup'
import RunDetail from './RunDetail.vue'
import RunStatusPill from './RunStatusPill.vue'

defineProps<{ canRun: boolean }>()
const emit = defineEmits<{ grace: [hours: number] }>()
const $q = useQuasar()
const notify = useNotify()
const now = useNow()
const gc = useAsync(() => call(api.GET('/maintenance/gc')))
const starting = ref(false)

const status = computed(() => gc.data.value)
watch(status, (s) => {
  if (s) emit('grace', s.grace_hours)
})
const forbidden = computed(() => gc.error.value instanceof ApiError && [403, 404].includes(gc.error.value.status))
const latest = computed(() => status.value?.runs[0] ?? null)
const lastDone = computed(() => status.value?.runs.find((r) => r.status === 'succeeded') ?? null)
const running = computed(() => latest.value?.status === 'running')
usePolling(running, () => gc.reload())

const columns = computed<QTableColumn<GcRun>[]>(() => [
  { name: 'status', label: t('cleanup.gc.col.status'), field: 'status', align: 'left' },
  { name: 'started', label: t('cleanup.gc.col.started'), field: 'started_at', align: 'left' },
  { name: 'duration', label: t('cleanup.gc.col.duration'), field: (r) => r.finished_at ?? '', align: 'left' },
  { name: 'freed', label: t('cleanup.gc.col.freed'), field: 'freed_bytes', align: 'right' },
  { name: 'marked', label: t('cleanup.gc.col.marked'), field: (r) => formatCount(r.marked), align: 'right', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'uploads', label: t('cleanup.gc.col.uploads'), field: (r) => formatCount(r.uploads_removed), align: 'right', classes: 'gt-sm', headerClasses: 'gt-sm' },
])

async function runNow(): Promise<void> {
  starting.value = true
  try {
    const job = await call(api.POST('/maintenance/gc/run'))
    notify.success(job.status === 'merged' ? t('cleanup.gc.merged') : t('cleanup.gc.started'))
    await gc.reload()
  } catch (error) {
    notify.error(error)
  } finally {
    starting.value = false
  }
}
</script>

<style scoped>
.gc__error {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-secondary);
}

.gc__stats {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  margin: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.gc__stat {
  padding: 10px 14px;
  border-left: 1px solid var(--border);
  min-width: 0;
}

.gc__stat:first-child {
  border-left: 0;
}

.gc__stat dt {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.gc__stat dd {
  margin: 4px 0 0;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
}

.gc__value {
  font-size: var(--fs-16);
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}

.gc__sub {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.gc__detail {
  width: 100%;
}

.gc__recent-title {
  margin: 16px 0 4px;
  font-size: var(--fs-13);
  font-weight: 600;
}

.gc__card {
  width: 100%;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}

@media (max-width: 1023px) {
  .gc__stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .gc__stat {
    border-left: 0;
    border-top: 1px solid var(--border);
  }

  .gc__stat:nth-child(-n + 2) {
    border-top: 0;
  }
}
</style>
