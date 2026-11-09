<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- One job run (side panel): status, progress, facts, the result figures, the error, and the log viewer. While the
     run is running, progress and new log lines arrive live from the `tasks` topic; when it ends the run is fetched
     again for its result and final log. -->
<template>
  <q-dialog v-model="open" position="right" full-height :maximized="$q.screen.lt.sm" transition-show="slide-left" transition-hide="slide-right">
    <q-card class="run-drawer" :aria-label="title">
      <header class="run-drawer__head">
        <div class="run-drawer__icon"><q-icon :name="iconTasks" size="22px" /></div>
        <div class="col">
          <div class="run-drawer__title">{{ title }}</div>
          <div v-if="run" class="text-caption mono">{{ run.task_id ?? run.kind }}</div>
        </div>
        <q-btn v-close-popup flat round dense :icon="iconClose" :aria-label="t('common.close')" />
      </header>

      <div v-if="detail.error.value && !run" class="run-drawer__body" role="alert">
        {{ t('tasks.run.loadFailed') }}: {{ errorMessage(detail.error.value) }}
      </div>
      <div v-else-if="!run" class="run-drawer__body"><q-skeleton height="160px" /></div>
      <div v-else class="run-drawer__body">
        <div class="run-drawer__status">
          <RunStatusPill :status="run.status" />
          <span class="ws-badge run-drawer__trigger">{{ triggerLabel(run.trigger) }}</span>
        </div>
        <RunDetail v-if="run.status === 'running'" :run="run" class="run-drawer__progress" />

        <dl class="run-drawer__facts">
          <dt>{{ t('tasks.run.started') }}</dt>
          <dd>{{ absoluteTime(run.started_at) }} <span class="text-faint">· <TimeCell :value="run.started_at" /></span></dd>
          <dt>{{ t('tasks.run.duration') }}</dt>
          <dd class="num">{{ formatDuration(runSeconds(run, now)) }}</dd>
          <dt>{{ t('tasks.run.attempt') }}</dt>
          <dd class="num">
            {{ run.attempt }}
            <q-btn flat dense no-caps size="sm" color="primary" :label="t('tasks.run.allAttempts')" @click="emit('attempts', run.job_id)" />
          </dd>
          <dt>{{ t('tasks.run.worker') }}</dt>
          <dd class="mono">{{ run.worker_id ?? '–' }}</dd>
          <dt>{{ t('tasks.run.job') }}</dt>
          <dd class="mono">{{ run.job_id }}</dd>
          <dt>{{ t('tasks.run.runId') }}</dt>
          <dd class="mono">{{ run.id }}</dd>
        </dl>

        <section v-if="run.error" class="run-drawer__section">
          <h3 class="run-drawer__h">{{ t('tasks.run.error') }}</h3>
          <div class="run-drawer__error mono">{{ run.error }}</div>
        </section>

        <section v-if="run.status !== 'running'" class="run-drawer__section">
          <h3 class="run-drawer__h">{{ t('tasks.run.result') }}</h3>
          <dl v-if="results.length" class="run-drawer__facts run-drawer__facts--result">
            <template v-for="r in results" :key="r.key">
              <dt>{{ r.key }}</dt>
              <dd class="mono">{{ r.value }}</dd>
            </template>
          </dl>
          <div v-else class="text-faint">{{ t('tasks.run.noResult') }}</div>
        </section>

        <LogViewer :lines="run.log" :live="run.status === 'running'" />
      </div>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, onScopeDispose, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { JobRun } from '@/api/types'
import { runSeconds } from '@/components/cleanup/cleanup'
import RunDetail from '@/components/cleanup/RunDetail.vue'
import RunStatusPill from '@/components/cleanup/RunStatusPill.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNow } from '@/composables/useNow'
import { t } from '@/i18n'
import { useLiveStore } from '@/stores/live'
import { absoluteTime, formatDuration } from '@/utils/format'
import { iconClose, iconTasks } from '@/utils/icons'

import LogViewer from './LogViewer.vue'
import { appendLog, applyRunEvent, kindLabel, resultEntries, triggerLabel } from './tasks'

const props = defineProps<{ runId: string | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ attempts: [jobId: string] }>()
const $q = useQuasar()
const now = useNow()
const live = useLiveStore()
const run = ref<JobRun | null>(null)

const detail = useAsync(async () => (props.runId && open.value ? call(api.GET('/tasks/runs/{run_id}', { params: { path: { run_id: props.runId } } })) : null), [() => props.runId, open])
watch(detail.data, (value) => {
  run.value = value ? { ...value, log: [...value.log] } : null
})

const title = computed(() => (run.value ? t('tasks.run.title', { task: kindLabel(run.value.kind) }) : t('tasks.run.log')))
const results = computed(() => resultEntries(run.value?.result ?? {}))

const off = live.onTasks((event) => {
  const current = run.value
  if (!current || !open.value) return
  if (event.type === 'job.log') appendLog(current, event)
  else if (event.type === 'job.progress' || event.type === 'job.update') {
    applyRunEvent([current], event)
    if (event.type === 'job.update' && event.run_id === current.id && event.status !== 'running') void detail.reload()
  }
})
onScopeDispose(off)
</script>

<style scoped>
.run-drawer {
  width: 600px;
  max-width: 100vw;
  height: 100%;
  display: flex;
  flex-direction: column;
}

.run-drawer__head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
}

.run-drawer__icon {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: var(--radius-sm);
  background: var(--surface-brand);
  color: var(--q-primary);
}

.run-drawer__title {
  font-weight: 600;
  font-size: var(--fs-16);
}

.run-drawer__body {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  padding: 16px 20px 24px;
}

.run-drawer__status {
  display: flex;
  gap: 8px;
}

.run-drawer__trigger {
  color: var(--text-secondary);
  box-shadow: inset 0 0 0 1px var(--border);
}

.run-drawer__progress {
  margin-top: 12px;
}

.run-drawer__facts {
  display: grid;
  grid-template-columns: minmax(100px, max-content) 1fr;
  gap: 8px 16px;
  margin: 16px 0 0;
  font-size: var(--fs-13);
}

.run-drawer__facts dt {
  color: var(--text-secondary);
}

.run-drawer__facts dd {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
}

.run-drawer__facts--result {
  margin-top: 0;
}

.run-drawer__section {
  margin-top: 24px;
}

.run-drawer__h {
  margin: 0 0 8px;
  font-size: var(--fs-14);
  font-weight: 600;
  line-height: 1.4;
}

.run-drawer__error {
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--status-blocked-bg);
  color: var(--status-blocked);
  font-size: var(--fs-12);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
