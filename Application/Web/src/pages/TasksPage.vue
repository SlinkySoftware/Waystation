<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Tasks and workers (UI_DESIGN §4.9): a summary strip, then tabs for scheduled tasks, job runs (live progress and a
     log drawer), workers with queues, and dead letters with retry. Tab, run filters and the open run live in the
     URL. Admin only (the server decides; the `tasks` live topic is admin-only too). -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.tasks')" :caption="t('tasks.caption')">
      <template #actions>
        <q-btn v-if="isAdmin" flat no-caps :icon="iconRefresh" :label="t('tasks.refresh')" :loading="refreshing" @click="refreshAll" />
      </template>
    </PageHeader>

    <EmptyState v-if="!isAdmin" illustration="notFound" :title="t('tasks.forbidden')" />
    <div v-else class="tasks">
      <TasksOverview :overview="workers.data.value" @select="(next) => (tab = next)" />

      <q-tabs :model-value="tab" dense no-caps align="left" active-color="primary" indicator-color="primary" narrow-indicator class="tasks__tabs" @update:model-value="(next: TasksTab) => (tab = next)">
        <q-tab name="scheduled" :label="t('tasks.tab.scheduled')" />
        <q-tab name="runs" :label="t('tasks.tab.runs')" />
        <q-tab name="workers" :label="t('tasks.tab.workers')" />
        <q-tab name="dead" :label="t('tasks.tab.dead')">
          <q-badge v-if="deadCount" color="negative" floating rounded :label="deadCount" />
        </q-tab>
      </q-tabs>

      <PageCard v-if="tab === 'scheduled'" :title="t('tasks.scheduled.title')" :caption="t('tasks.scheduled.caption')" flush>
        <EmptyState v-if="tasks.error.value && !tasks.data.value" illustration="error" :title="t('tasks.loadFailed')" :text="errorMessage(tasks.error.value)">
          <q-btn unelevated no-caps color="primary" :label="t('common.retry')" @click="tasks.reload" />
        </EmptyState>
        <ScheduledTasksTable v-else :tasks="tasks.data.value?.items ?? []" :loading="tasks.loading.value && !tasks.data.value" @run="runTask" @edit="editTask" @history="showRuns" @toggle="toggleTask">
          <template #empty>
            <EmptyState class="full-width" illustration="activity" :title="t('tasks.scheduled.emptyTitle')" :text="t('tasks.scheduled.emptyText')" />
          </template>
        </ScheduledTasksTable>
      </PageCard>
      <RunsTable v-else-if="tab === 'runs'" @open="(id) => (runId = id)" />
      <WorkersPanel v-else-if="tab === 'workers'" :overview="workers.data.value" :loaded-at="workersLoadedAt" @open="(id) => (runId = id)" />
      <DeadLettersTable v-else :page="dead.page.value" :rows="dead.rows.value" :loading="dead.loading.value" :busy="dead.busy.value" @retry="dead.retry" @discard="dead.discard" @more="dead.more" />
    </div>

    <ScheduleDialog v-model="scheduleOpen" :task="editing" @saved="onChanged" />
    <RunDrawer v-model="drawerOpen" :run-id="runId" @attempts="showAttempts" />
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, onScopeDispose, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { ScheduledTask } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import DeadLettersTable from '@/components/tasks/DeadLettersTable.vue'
import RunDrawer from '@/components/tasks/RunDrawer.vue'
import RunsTable from '@/components/tasks/RunsTable.vue'
import ScheduledTasksTable from '@/components/tasks/ScheduledTasksTable.vue'
import ScheduleDialog from '@/components/tasks/ScheduleDialog.vue'
import { TASKS_TABS, taskLabel, type TasksTab } from '@/components/tasks/tasks'
import TasksOverview from '@/components/tasks/TasksOverview.vue'
import { useDeadLetters } from '@/components/tasks/useDeadLetters'
import WorkersPanel from '@/components/tasks/WorkersPanel.vue'
import { useAsync } from '@/composables/useAsync'
import { useLiveTopic } from '@/composables/useLiveTopic'
import { useNotify } from '@/composables/useNotify'
import { usePolling } from '@/composables/usePolling'
import { useQueryNavigate, useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useLiveStore } from '@/stores/live'
import { iconRefresh } from '@/utils/icons'

const $q = useQuasar()
const notify = useNotify()
const auth = useAuthStore()
const live = useLiveStore()
const navigate = useQueryNavigate()
const isAdmin = computed(() => auth.can('admin'))

const tabParam = useQueryParam('tab')
const tab = computed<TasksTab>({
  get: () => (TASKS_TABS as readonly string[]).includes(tabParam.value ?? '') ? (tabParam.value as TasksTab) : 'scheduled',
  set: (next) => navigate({ tab: next === 'scheduled' ? null : next, status: null, kind: null, task: null, job: null }),
})
const runParam = useQueryParam('run')
const runId = computed<string | null>({ get: () => runParam.value, set: (id) => (runParam.value = id) })
const drawerOpen = computed({ get: () => Boolean(runId.value), set: (on: boolean) => !on && (runId.value = null) })

const tasks = useAsync(() => (isAdmin.value ? call(api.GET('/tasks/scheduled')) : Promise.resolve(null)), [isAdmin])
const workers = useAsync(() => (isAdmin.value ? call(api.GET('/tasks/workers')) : Promise.resolve(null)), [isAdmin])
const workersLoadedAt = ref(Date.now())
watch(workers.data, () => (workersLoadedAt.value = Date.now()))
const dead = useDeadLetters(isAdmin, () => void workers.reload())
const deadCount = computed(() => workers.data.value?.dead_letters ?? dead.page.value?.total ?? 0)

useLiveTopic(() => (isAdmin.value ? ['tasks'] : []))
usePolling(isAdmin, () => workers.reload(), 10_000) // heartbeats and queue depth have no events

const refreshing = ref(false)
async function refreshAll(): Promise<void> {
  refreshing.value = true
  await Promise.all([tasks.reload(), workers.reload(), dead.reload()])
  refreshing.value = false
}

// Live: a job started or ended -> last runs, running jobs and (when it died) the dead letters change.
let timer: ReturnType<typeof setTimeout> | null = null
let deadChanged = false
const off = live.onTasks((event) => {
  if (event.type === 'worker.update') notify.success(t(event.state === 'online' ? 'tasks.workers.onlineToast' : 'tasks.workers.offlineToast', { id: event.worker_id }))
  else if (event.type !== 'job.update') return
  else if (event.status === 'dead') deadChanged = true
  timer ??= setTimeout(() => {
    timer = null
    void tasks.reload()
    void workers.reload()
    if (deadChanged) void dead.reload()
    deadChanged = false
  }, 500)
})
onScopeDispose(() => {
  off()
  if (timer) clearTimeout(timer)
})

// ---------------------------------------------------------------- scheduled task actions
const scheduleOpen = ref(false)
const editing = ref<ScheduledTask | null>(null)

function editTask(task: ScheduledTask): void {
  editing.value = task
  scheduleOpen.value = true
}

function onChanged(): void {
  void tasks.reload()
}

function showRuns(task: ScheduledTask): void {
  navigate({ tab: 'runs', task: task.id, status: null, kind: null, job: null })
}

function showAttempts(jobId: string): void {
  navigate({ tab: 'runs', job: jobId, task: null, status: null, kind: null, run: null })
}

async function runTask(task: ScheduledTask): Promise<void> {
  try {
    const job = await call(api.POST('/tasks/scheduled/{task_id}/run', { params: { path: { task_id: task.id } } }))
    notify.success(t(job.status === 'merged' ? 'tasks.scheduled.merged' : 'tasks.scheduled.queued', { task: taskLabel(task) }))
    await Promise.all([tasks.reload(), workers.reload()])
  } catch (error) {
    notify.error(error)
  }
}

async function setEnabled(task: ScheduledTask, enabled: boolean): Promise<void> {
  try {
    await call(api.PATCH('/tasks/scheduled/{task_id}', { params: { path: { task_id: task.id } }, body: { enabled } }))
    notify.success(t(enabled ? 'tasks.scheduled.enabledToast' : 'tasks.scheduled.pausedToast', { task: taskLabel(task) }))
    await tasks.reload()
  } catch (error) {
    notify.error(error)
  }
}

function toggleTask(task: ScheduledTask, enabled: boolean): void {
  if (enabled || !task.routine) {
    void setEnabled(task, enabled)
    return
  }
  $q.dialog({
    title: t('tasks.scheduled.disableRoutineTitle', { task: taskLabel(task) }),
    message: t('tasks.scheduled.disableRoutineText'),
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'negative', label: t('tasks.scheduled.pause') },
  }).onOk(() => void setEnabled(task, false))
}
</script>

<style scoped>
.tasks {
  display: grid;
  gap: 16px;
}

.tasks__tabs {
  border-bottom: 1px solid var(--border);
}
</style>
