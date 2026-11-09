<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Workers (UI_DESIGN §4.9): one card per worker (host, version, queues, what it runs now, heartbeat with health)
     and the queue table (waiting, running, delayed, oldest waiting, which workers consume it). Heartbeat ages count
     up between refreshes. -->
<template>
  <div class="workers">
    <PageCard :title="t('tasks.workers.title')" :caption="t('tasks.workers.caption')">
      <q-skeleton v-if="!overview" height="120px" />
      <EmptyState v-else-if="!overview.workers.length" compact illustration="error" :title="t('tasks.workers.emptyTitle')" :text="t('tasks.workers.emptyText')" />
      <div v-else class="workers__grid">
        <article v-for="w in overview.workers" :key="w.id" class="workers__card" :class="`workers__card--${w.state}`">
          <header class="workers__head">
            <q-icon :name="iconWorker" size="22px" class="workers__icon" />
            <div class="col">
              <div class="workers__host">{{ w.hostname }}</div>
              <div class="workers__sub mono">{{ w.id }}</div>
            </div>
            <span v-if="w.leader" class="ws-badge tone-hit"><q-icon :name="iconLeader" />{{ t('tasks.workers.leader') }}<q-tooltip>{{ t('tasks.workers.leaderHelp') }}</q-tooltip></span>
            <span class="ws-badge" :class="workerStateMeta(w.state).tone"><q-icon :name="workerStateMeta(w.state).icon" />{{ workerStateMeta(w.state).label }}<q-tooltip>{{ workerStateMeta(w.state).description }}</q-tooltip></span>
          </header>
          <div class="workers__facts">
            <span>{{ t('tasks.workers.version', { version: w.version }) }}</span>
            <span v-if="w.concurrency">{{ t('tasks.workers.concurrency', { n: w.concurrency }) }}</span>
            <span>{{ t('tasks.workers.heartbeat', { age: formatAge(heartbeatAge(w, loadedAt, now)) }) }}</span>
            <span v-if="w.started_at">{{ t('tasks.workers.started', { time: relativeTime(w.started_at, now) }) }}</span>
          </div>
          <div class="workers__queues" :aria-label="t('tasks.workers.queues')">
            <span v-for="q in w.queues" :key="q" class="workers__chip">{{ queueLabel(q) }}</span>
          </div>
          <div class="workers__running">
            <div class="workers__label">{{ t('tasks.workers.running') }}</div>
            <div v-if="!w.running_jobs.length" class="text-faint">{{ t('tasks.workers.idle') }}</div>
            <button v-for="j in w.running_jobs" :key="j.job_id" type="button" class="workers__job" :disabled="!j.run_id" @click="j.run_id && emit('open', j.run_id)">
              <q-icon :name="iconSync" class="workers__spin" />
              <span class="workers__job-name">{{ kindLabel(j.kind) }}</span>
              <TimeCell v-if="j.started_at" :value="j.started_at" class="workers__sub" />
            </button>
          </div>
        </article>
      </div>
    </PageCard>

    <PageCard :title="t('tasks.workers.queuesTitle')" :caption="t('tasks.workers.queuesCaption')" flush>
      <q-table class="ws-table" flat dense :rows="overview?.queues ?? []" :columns="columns" row-key="name" :loading="!overview" :pagination="{ rowsPerPage: 0 }" hide-pagination>
        <template #body-cell-name="{ row }">
          <q-td>
            <span class="workers__queue-name">{{ queueLabel(row.name) }}</span>
            <span class="workers__sub mono"> {{ row.name }}</span>
          </q-td>
        </template>
        <template #body-cell-consumers="{ row }">
          <q-td>
            <span v-if="consumers(row.name).length">{{ consumers(row.name).join(', ') }}</span>
            <span v-else class="workers__warn"><q-icon :name="iconWarning" /> {{ t('tasks.workers.noConsumer') }}</span>
          </q-td>
        </template>
      </q-table>
    </PageCard>
  </div>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed } from 'vue'

import type { QueueFigures, WorkerOverview } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useNow } from '@/composables/useNow'
import { t } from '@/i18n'
import { formatCount, relativeTime } from '@/utils/format'
import { iconLeader, iconSync, iconWarning, iconWorker } from '@/utils/icons'

import { formatAge, heartbeatAge, kindLabel, queueLabel, workerStateMeta } from './tasks'

const props = defineProps<{ overview: WorkerOverview | null; loadedAt: number }>()
const emit = defineEmits<{ open: [runId: string] }>()
const now = useNow()

function consumers(queue: string): string[] {
  return (props.overview?.workers ?? []).filter((w) => w.state !== 'lost' && w.queues.includes(queue)).map((w) => w.hostname)
}

const columns = computed<QTableColumn<QueueFigures>[]>(() => [
  { name: 'name', label: t('tasks.workers.col.queue'), field: 'name', align: 'left' },
  { name: 'waiting', label: t('tasks.workers.col.waiting'), field: (r) => formatCount(r.waiting), align: 'right' },
  { name: 'running', label: t('tasks.workers.col.running'), field: (r) => formatCount(r.running), align: 'right' },
  { name: 'delayed', label: t('tasks.workers.col.delayed'), field: (r) => formatCount(r.delayed), align: 'right', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'oldest', label: t('tasks.workers.col.oldest'), field: (r) => (r.waiting ? formatAge(r.oldest_waiting_age_s) : '–'), align: 'right', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'consumers', label: t('tasks.workers.col.consumers'), field: 'name', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
])
</script>

<style scoped>
.workers {
  display: grid;
  gap: 16px;
}

.workers__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
}

.workers__card {
  display: grid;
  gap: 10px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-raised);
}

.workers__card--late {
  border-color: var(--status-degraded);
}

.workers__card--lost {
  border-color: var(--status-blocked);
}

.workers__head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.workers__icon {
  color: var(--q-primary);
}

.workers__host {
  font-weight: 600;
}

.workers__sub {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.workers__facts {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.workers__queues {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.workers__chip {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: var(--fs-12);
  box-shadow: inset 0 0 0 1px var(--border);
}

.workers__label {
  font-size: var(--fs-12);
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 4px;
}

.workers__job {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 40px;
  padding: 0 8px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.workers__job:hover:not(:disabled),
.workers__job:focus-visible {
  background: var(--surface-subtle);
}

.workers__job-name {
  flex: 1;
}

.workers__spin {
  color: var(--cache-hit);
  animation: workers-spin 1.4s linear infinite;
}

@keyframes workers-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .workers__spin {
    animation: none;
  }
}

.workers__queue-name {
  font-weight: 600;
}

.workers__warn {
  color: var(--status-degraded);
}
</style>
