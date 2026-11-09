<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- The Tasks page's summary strip: workers online, jobs running, jobs waiting (with the oldest wait) and dead
     letters. Each tile opens its tab; problems (no worker, dead letters) are tinted and carry an icon. -->
<template>
  <div class="overview" role="list">
    <button v-for="tile in tiles" :key="tile.tab" type="button" role="listitem" class="overview__tile" :class="tile.tone" @click="emit('select', tile.tab)">
      <span class="overview__label"><q-icon :name="tile.icon" size="16px" />{{ tile.label }}</span>
      <span class="overview__value num">{{ tile.value }}</span>
      <span class="overview__sub">{{ tile.sub }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { WorkerOverview } from '@/api/types'
import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconDeadLetters, iconSync, iconWaiting, iconWorker } from '@/utils/icons'

import { formatAge, type TasksTab } from './tasks'

const props = defineProps<{ overview: WorkerOverview | null }>()
const emit = defineEmits<{ select: [tab: TasksTab] }>()

const tiles = computed(() => {
  const o = props.overview
  const online = o ? o.workers.filter((w) => w.state !== 'lost').length : null
  const running = o ? o.queues.reduce((a, q) => a + q.running, 0) : null
  const waiting = o ? o.queues.reduce((a, q) => a + q.waiting, 0) : null
  const oldest = o ? Math.max(0, ...o.queues.map((q) => q.oldest_waiting_age_s)) : 0
  const dead = o?.dead_letters ?? null
  const show = (n: number | null) => (n === null ? '–' : formatCount(n))
  return [
    { tab: 'workers' as const, icon: iconWorker, label: t('tasks.overview.workers'), value: show(online), sub: online ? t('tasks.overview.workersLine', { count: online, n: online }) : t('tasks.overview.workersNone'), tone: o && !online ? 'overview__tile--bad' : '' },
    { tab: 'runs' as const, icon: iconSync, label: t('tasks.overview.running'), value: show(running), sub: t('tasks.overview.runningLine', { count: running ?? 0, n: running ?? 0 }), tone: '' },
    { tab: 'workers' as const, icon: iconWaiting, label: t('tasks.overview.waiting'), value: show(waiting), sub: waiting ? t('tasks.overview.waitingLine', { age: formatAge(oldest) }) : t('tasks.overview.waitingNone'), tone: '' },
    { tab: 'dead' as const, icon: iconDeadLetters, label: t('tasks.overview.dead'), value: show(dead), sub: dead ? t('tasks.overview.deadLine') : t('tasks.overview.deadNone'), tone: dead ? 'overview__tile--bad' : '' },
  ]
})
</script>

<style scoped>
.overview {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.overview__tile {
  display: grid;
  gap: 2px;
  min-height: 40px;
  padding: 12px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md, var(--radius-sm));
  background: var(--surface-raised);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color 150ms ease-out;
}

.overview__tile:hover,
.overview__tile:focus-visible {
  border-color: var(--q-primary);
}

.overview__label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.overview__value {
  font-size: var(--fs-24);
  font-weight: 650;
  line-height: 1.2;
}

.overview__sub {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.overview__tile--bad {
  border-color: var(--status-blocked);
}

.overview__tile--bad .overview__value,
.overview__tile--bad .overview__label {
  color: var(--status-blocked);
}

@media (max-width: 1023px) {
  .overview {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
