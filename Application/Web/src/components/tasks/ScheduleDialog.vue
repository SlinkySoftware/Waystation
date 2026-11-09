<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Edit a scheduled task's schedule: cron / macro / @every with presets and a plain-language reading. The server
     validates for real; its field error is shown under the input. -->
<template>
  <FormDialog v-model="open" :title="task ? t('tasks.schedule.title', { task: taskLabel(task) }) : ''" :caption="t('tasks.scheduled.caption')" :saving="saving" :submit-label="t('tasks.schedule.save')" @submit="save">
    <div class="schedule-dialog">
      <q-input
        v-model="text"
        outlined
        autofocus
        class="mono"
        :label="t('tasks.schedule.label')"
        :hint="t('tasks.schedule.hint')"
        :rules="[(v: string) => scheduleError(v) ?? true]"
        :error="Boolean(serverError)"
        :error-message="serverError ?? undefined"
        lazy-rules
        @update:model-value="serverError = null"
      />
      <div>
        <div class="schedule-dialog__label">{{ t('tasks.schedule.presets') }}</div>
        <div class="schedule-dialog__presets">
          <q-chip v-for="p in SCHEDULE_PRESETS" :key="p" clickable dense outline :selected="text === p" class="mono" @click="pick(p)">
            {{ p }}
            <q-tooltip>{{ describeTaskSchedule(p) }}</q-tooltip>
          </q-chip>
        </div>
      </div>
      <div class="schedule-dialog__reading" aria-live="polite">
        <q-icon :name="iconTasks" size="16px" />
        {{ scheduleError(text) ? scheduleError(text) : t('tasks.schedule.reading', { when: describeTaskSchedule(text.trim()) }) }}
      </div>
    </div>
  </FormDialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

import { api, ApiError, call } from '@/api/client'
import type { ScheduledTask } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { iconTasks } from '@/utils/icons'

import { describeTaskSchedule, SCHEDULE_PRESETS, scheduleError, taskLabel } from './tasks'

const props = defineProps<{ task: ScheduledTask | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [task: ScheduledTask] }>()
const notify = useNotify()
const text = ref('')
const saving = ref(false)
const serverError = ref<string | null>(null)

watch(open, (isOpen) => {
  if (!isOpen || !props.task) return
  text.value = props.task.schedule
  serverError.value = null
})

function pick(preset: string): void {
  text.value = preset
  serverError.value = null
}

async function save(): Promise<void> {
  if (!props.task) return
  saving.value = true
  try {
    const updated = await call(api.PATCH('/tasks/scheduled/{task_id}', { params: { path: { task_id: props.task.id } }, body: { schedule: text.value } }))
    notify.success(t('tasks.schedule.saved', { task: taskLabel(updated) }))
    emit('saved', updated)
    open.value = false
  } catch (error) {
    const field = error instanceof ApiError ? error.fieldErrors.schedule : undefined
    if (field) serverError.value = field
    else notify.error(error)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.schedule-dialog {
  display: grid;
  gap: 16px;
}

.schedule-dialog__label {
  font-size: var(--fs-13);
  font-weight: 600;
  margin-bottom: 4px;
}

.schedule-dialog__presets {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.schedule-dialog__reading {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}
</style>
