<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Row actions of a scheduled task: run now, its runs, and edit schedule (or open the owning cleanup policy).
     Labelled icon buttons, touch targets ≥ 40 px. -->
<template>
  <span class="task-actions">
    <q-btn flat round dense :icon="iconPlay" :disable="running" :aria-label="`${t('tasks.scheduled.runNow')}: ${label}`" @click="emit('run')">
      <q-tooltip>{{ t('tasks.scheduled.runNow') }}</q-tooltip>
    </q-btn>
    <q-btn flat round dense :icon="iconHistory" :aria-label="`${t('tasks.scheduled.history')}: ${label}`" @click="emit('history')">
      <q-tooltip>{{ t('tasks.scheduled.history') }}</q-tooltip>
    </q-btn>
    <q-btn
      v-if="task.owner"
      flat
      round
      dense
      :icon="iconExternal"
      :to="{ name: 'cleanup', query: { edit: task.owner.id } }"
      :aria-label="`${t('tasks.scheduled.openPolicy')}: ${label}`"
    >
      <q-tooltip>{{ t('tasks.scheduled.openPolicy') }}</q-tooltip>
    </q-btn>
    <q-btn v-else flat round dense :icon="iconEdit" :aria-label="`${t('tasks.scheduled.editSchedule')}: ${label}`" @click="emit('edit')">
      <q-tooltip>{{ t('tasks.scheduled.editSchedule') }}</q-tooltip>
    </q-btn>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { ScheduledTask } from '@/api/types'
import { t } from '@/i18n'
import { iconEdit, iconExternal, iconHistory, iconPlay } from '@/utils/icons'

import { taskLabel } from './tasks'

const props = defineProps<{ task: ScheduledTask }>()
const emit = defineEmits<{ run: []; edit: []; history: [] }>()
const running = computed(() => props.task.last_run?.status === 'running')
const label = computed(() => taskLabel(props.task))
</script>

<style scoped>
.task-actions {
  display: inline-flex;
  gap: 2px;
}
</style>
