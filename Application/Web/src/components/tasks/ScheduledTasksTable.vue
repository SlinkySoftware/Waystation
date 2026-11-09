<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Scheduled tasks (UI_DESIGN §4.9): task, schedule in words, last run (status, when, progress while running),
     next run, enabled; run now / edit schedule / runs. A cleanup policy's task links to its policy. Cards on phones. -->
<template>
  <q-table
    class="ws-table scheduled"
    flat
    :rows="tasks"
    :columns="columns"
    row-key="id"
    :loading="loading"
    :grid="$q.screen.lt.sm"
    :pagination="{ rowsPerPage: 0 }"
    hide-pagination
  >
    <template #body-cell-task="{ row }">
      <q-td :class="{ 'scheduled--off': !row.enabled }">
        <div class="scheduled__name">
          {{ taskLabel(row) }}
          <span v-if="row.routine" class="ws-badge scheduled__routine">{{ t('tasks.scheduled.routine') }}<q-tooltip max-width="320px">{{ t('tasks.scheduled.routineHelp') }}</q-tooltip></span>
        </div>
        <div class="scheduled__sub mono">{{ row.id }}</div>
        <div v-if="kindHelp(row.kind)" class="scheduled__sub">{{ kindHelp(row.kind) }}</div>
      </q-td>
    </template>
    <template #body-cell-schedule="{ row }">
      <q-td class="scheduled__schedule">
        <div>{{ describeTaskSchedule(row.schedule) }}</div>
        <div class="scheduled__sub mono">{{ row.schedule }}</div>
      </q-td>
    </template>
    <template #body-cell-lastRun="{ row }">
      <q-td class="scheduled__last">
        <template v-if="row.last_run">
          <div class="scheduled__run-head">
            <RunStatusPill :status="row.last_run.status" />
            <TimeCell :value="row.last_run.started_at" />
          </div>
          <RunDetail v-if="row.last_run.status !== 'succeeded'" :run="row.last_run" />
        </template>
        <span v-else class="text-faint">{{ t('tasks.scheduled.never') }}</span>
      </q-td>
    </template>
    <template #body-cell-nextRun="{ row }">
      <q-td>
        <TimeCell v-if="row.next_run_at" :value="row.next_run_at" />
        <span v-else class="text-faint">{{ t('tasks.scheduled.paused') }}</span>
      </q-td>
    </template>
    <template #body-cell-enabled="{ row }">
      <q-td>
        <q-toggle :model-value="row.enabled" dense :disable="Boolean(row.owner)" :aria-label="taskLabel(row)" @update:model-value="(on: boolean) => emit('toggle', row, on)">
          <q-tooltip v-if="row.owner">{{ t('tasks.scheduled.ownedHelp') }}</q-tooltip>
        </q-toggle>
      </q-td>
    </template>
    <template #body-cell-actions="{ row }">
      <q-td class="text-right scheduled__actions">
        <TaskActions :task="row" @run="emit('run', row)" @edit="emit('edit', row)" @history="emit('history', row)" />
      </q-td>
    </template>

    <template #item="{ row }">
      <div class="scheduled__card" :class="{ 'scheduled--off': !row.enabled }">
        <div class="scheduled__card-head">
          <div class="col">
            <div class="scheduled__name">{{ taskLabel(row) }}</div>
            <div class="scheduled__sub">{{ describeTaskSchedule(row.schedule) }}</div>
          </div>
          <q-toggle :model-value="row.enabled" dense :disable="Boolean(row.owner)" :aria-label="taskLabel(row)" @update:model-value="(on: boolean) => emit('toggle', row, on)" />
        </div>
        <div v-if="row.last_run" class="scheduled__run-head">
          <RunStatusPill :status="row.last_run.status" :tooltip="false" />
          <TimeCell :value="row.last_run.started_at" />
        </div>
        <RunDetail v-if="row.last_run && row.last_run.status !== 'succeeded'" :run="row.last_run" />
        <TaskActions :task="row" @run="emit('run', row)" @edit="emit('edit', row)" @history="emit('history', row)" />
      </div>
    </template>

    <template #no-data>
      <slot name="empty" />
    </template>
  </q-table>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed } from 'vue'

import type { ScheduledTask } from '@/api/types'
import RunDetail from '@/components/cleanup/RunDetail.vue'
import RunStatusPill from '@/components/cleanup/RunStatusPill.vue'
import TimeCell from '@/components/TimeCell.vue'
import { t } from '@/i18n'

import TaskActions from './TaskActions.vue'
import { describeTaskSchedule, kindHelp, taskLabel } from './tasks'

defineProps<{ tasks: ScheduledTask[]; loading: boolean }>()
const emit = defineEmits<{ run: [task: ScheduledTask]; edit: [task: ScheduledTask]; history: [task: ScheduledTask]; toggle: [task: ScheduledTask, enabled: boolean] }>()
const $q = useQuasar()

const columns = computed<QTableColumn<ScheduledTask>[]>(() => [
  { name: 'task', label: t('tasks.scheduled.col.task'), field: 'id', align: 'left' },
  { name: 'schedule', label: t('tasks.scheduled.col.schedule'), field: 'schedule', align: 'left' },
  { name: 'lastRun', label: t('tasks.scheduled.col.lastRun'), field: (r) => r.last_run?.started_at ?? '', align: 'left' },
  { name: 'nextRun', label: t('tasks.scheduled.col.nextRun'), field: (r) => r.next_run_at ?? '', align: 'left' },
  { name: 'enabled', label: t('tasks.scheduled.col.enabled'), field: 'enabled', align: 'left' },
  { name: 'actions', label: '', field: 'id', align: 'right' },
])
</script>

<style scoped>
.scheduled__name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}

.scheduled__sub {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  white-space: normal;
  max-width: 320px;
}

.scheduled--off .scheduled__name {
  color: var(--text-secondary);
}

.scheduled__routine {
  font-weight: 500;
  color: var(--text-secondary);
  background: var(--surface-subtle);
  box-shadow: inset 0 0 0 1px var(--border);
}

.scheduled__schedule {
  white-space: normal;
  min-width: 150px;
}

.scheduled__last {
  min-width: 170px;
  white-space: normal;
}

.scheduled__run-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
}

.scheduled__actions {
  white-space: nowrap;
  padding-left: 0 !important;
  padding-right: 8px !important;
}

.scheduled__card {
  width: 100%;
  display: grid;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.scheduled__card-head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
</style>
