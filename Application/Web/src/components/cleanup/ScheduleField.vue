<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- When a policy runs: Manual / Daily / Weekly presets or a custom cron (UTC), with a plain-language reading. -->
<template>
  <div class="schedule-field">
    <div class="schedule-field__label">{{ t('cleanup.schedule.label') }}</div>
    <q-btn-toggle
      :model-value="mode"
      no-caps
      unelevated
      dense
      toggle-color="primary"
      class="schedule-field__toggle"
      :options="options"
      @update:model-value="choose"
    />
    <q-input
      v-if="mode === 'custom'"
      v-model="cron"
      outlined
      dense
      class="schedule-field__cron mono"
      :label="t('cleanup.schedule.cronLabel')"
      :hint="t('cleanup.schedule.cronHint')"
      :rules="[(v: string) => cronError(v) ?? true]"
      :error="Boolean(error)"
      :error-message="error"
      lazy-rules
    />
    <div class="schedule-field__reading">
      <q-icon :name="iconTasks" size="16px" />
      {{ describeSchedule(model) }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { t } from '@/i18n'
import { iconTasks } from '@/utils/icons'

import { cronError, describeSchedule, SCHEDULE_PRESETS, type ScheduleMode, scheduleMode } from './cleanup'

defineProps<{ error?: string }>()
const model = defineModel<string | null>({ required: true })

/** Custom stays selected while the user types, even if the text happens to equal a preset. */
const customPinned = defineModel<boolean>('custom', { default: false })
const mode = computed<ScheduleMode>(() => (customPinned.value ? 'custom' : scheduleMode(model.value)))

const options = computed(() =>
  (['manual', 'daily', 'weekly', 'custom'] as const).map((value) => ({ value, label: t(`cleanup.schedule.${value}`) })),
)

const cron = computed({
  get: () => model.value ?? '',
  set: (value: string) => {
    model.value = value.trim() ? value.trim() : ''
  },
})

function choose(next: ScheduleMode): void {
  customPinned.value = next === 'custom'
  if (next === 'manual') model.value = null
  else if (next === 'custom') model.value = model.value ?? SCHEDULE_PRESETS.daily
  else model.value = SCHEDULE_PRESETS[next]
}
</script>

<style scoped>
.schedule-field {
  display: grid;
  gap: 8px;
}

.schedule-field__label {
  font-size: var(--fs-13);
  font-weight: 600;
}

.schedule-field__toggle {
  justify-self: start;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.schedule-field__cron {
  max-width: 360px;
}

.schedule-field__reading {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}
</style>
