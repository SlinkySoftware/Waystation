<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- The repository form's Prefetch fields (proxy repositories of PREFETCH_FORMATS): keep popular metadata fresh, the
     warm list (one item per line, in the format's syntax) and when it runs. Client checks block saving; the server's
     field errors (`proxy.prefetch.*`, per item `proxy.prefetch.warm.<i>`) show on the fields. -->
<template>
  <div class="prefetch-fields">
    <div>
      <q-toggle v-model="model.refreshPopular" :label="t('prefetch.refreshPopular')" data-test="prefetch-refresh" />
      <div class="text-caption text-muted prefetch-fields__hint">{{ t('prefetch.refreshPopularHint') }}</div>
      <div v-if="errors['proxy.prefetch.refresh_popular']" class="text-caption text-negative" role="alert">{{ errors['proxy.prefetch.refresh_popular'] }}</div>
    </div>
    <q-input
      v-model="model.warmText"
      outlined
      dense
      autogrow
      type="textarea"
      :label="t('prefetch.warm')"
      :placeholder="t(`prefetch.warmPlaceholder.${fmt}`)"
      :hint="t(`prefetch.warmHint.${fmt}`)"
      :rules="[() => warmClientError || true]"
      :error="!!warmServerError"
      :error-message="warmServerError || warmClientError"
      spellcheck="false"
      autocomplete="off"
      class="prefetch-fields__warm"
      data-test="prefetch-warm"
    >
      <template #counter>
        <span :class="{ 'text-negative': count > WARM_MAX_ITEMS }" data-test="prefetch-count">{{ t('prefetch.warmCount', { n: count, max: WARM_MAX_ITEMS }) }}</span>
      </template>
    </q-input>
    <div class="prefetch-fields__schedule">
      <q-input
        v-model="model.prefetchSchedule"
        outlined
        dense
        :label="t('prefetch.schedule')"
        :hint="reading ?? t('prefetch.scheduleHint')"
        :rules="[() => clientErrors['proxy.prefetch.schedule'] ?? true]"
        :error="!!errors['proxy.prefetch.schedule']"
        :error-message="errors['proxy.prefetch.schedule'] || clientErrors['proxy.prefetch.schedule']"
        spellcheck="false"
        autocomplete="off"
        class="mono-input"
        data-test="prefetch-schedule"
      />
      <div class="prefetch-fields__presets">
        <span class="text-caption text-muted">{{ t('prefetch.presets') }}</span>
        <q-chip v-for="p in SCHEDULE_EXAMPLES" :key="p" dense clickable outline :label="p" class="mono" @click="model.prefetchSchedule = p" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { describeTaskSchedule } from '@/components/tasks/tasks'
import { t } from '@/i18n'

import { type FormFormat, parseWarm, prefetchErrors, type RepoFormModel, WARM_MAX_ITEMS } from './form'

type PrefetchFormat = 'docker' | 'npm' | 'raw' | 'maven' | 'go' | 'cargo'

const props = withDefaults(defineProps<{ errors?: Record<string, string> }>(), { errors: () => ({}) })
const model = defineModel<RepoFormModel>({ required: true })

const SCHEDULE_EXAMPLES = ['@daily', '0 3 * * *', '@every 6h', '0 4 * * 1']

const fmt = computed<PrefetchFormat>(() => model.value.format as FormFormat as PrefetchFormat)
const items = computed(() => parseWarm(model.value.warmText))
const count = computed(() => items.value.length)
const clientErrors = computed(() => prefetchErrors(model.value))

/** One message for the list: the first item (or list) error, naming the item it is about. */
function warmError(errors: Record<string, string>, list: string[]): string {
  if (errors['proxy.prefetch.warm']) return errors['proxy.prefetch.warm']
  const entry = Object.entries(errors).find(([field]) => /^proxy\.prefetch\.warm\.\d+$/.test(field))
  if (!entry) return ''
  const item = list[Number(entry[0].split('.').pop())]
  return item ? t('prefetch.error.item', { item, message: entry[1] }) : entry[1]
}

const warmServerError = computed(() => warmError(props.errors, items.value))
const warmClientError = computed(() => warmError(clientErrors.value, items.value))

const reading = computed(() => {
  const schedule = model.value.prefetchSchedule.trim()
  if (!schedule || clientErrors.value['proxy.prefetch.schedule']) return null
  const when = describeTaskSchedule(schedule)
  return t('prefetch.scheduleReading', { when: when.charAt(0).toLowerCase() + when.slice(1) })
})
</script>

<style scoped>
.prefetch-fields {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.prefetch-fields__hint {
  margin: -4px 0 0 46px;
}

.prefetch-fields__warm :deep(textarea) {
  font-family: var(--font-mono);
  font-size: var(--fs-12);
  min-height: 72px;
  max-height: 260px;
}

.prefetch-fields__schedule {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.prefetch-fields__presets {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px 4px;
  margin-top: 14px;
}

.prefetch-fields__presets .q-chip {
  margin: 0;
}

.mono-input :deep(input) {
  font-family: var(--font-mono);
  font-size: var(--fs-13);
}
</style>
