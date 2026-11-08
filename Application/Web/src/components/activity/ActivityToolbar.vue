<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Activity filters: event kind, repository, format, user/address, cache state, outcome, time range (preset or custom). -->
<template>
  <div class="act-toolbar" role="search" :aria-label="t('activity.filters')">
    <q-input
      :model-value="principal"
      dense
      outlined
      clearable
      debounce="300"
      maxlength="128"
      :placeholder="t('activity.userLabel')"
      :aria-label="t('activity.userLabel')"
      class="act-toolbar__user"
      @update:model-value="(v) => set({ principal: v ? String(v).trim() || null : null })"
    >
      <template #prepend><q-icon :name="iconPerson" size="18px" /></template>
    </q-input>
    <q-select :model-value="modelValue.kind" dense outlined clearable emit-value map-options options-dense :options="kindOptions" :label="t('activity.kindLabel')" class="act-toolbar__filter" @update:model-value="(v) => set({ kind: v ?? null })" />
    <q-select :model-value="modelValue.repo" dense outlined clearable emit-value map-options options-dense use-input input-debounce="0" :options="repoOptionsFiltered" :label="t('activity.repoLabel')" class="act-toolbar__filter act-toolbar__filter--wide" @filter="filterRepos" @update:model-value="(v) => set({ repo: v ?? null })" />
    <q-select :model-value="modelValue.format" dense outlined clearable emit-value map-options options-dense :options="formatOptions" :label="t('activity.formatLabel')" class="act-toolbar__filter" @update:model-value="(v) => set({ format: v ?? null })" />
    <q-select :model-value="modelValue.cache" dense outlined clearable emit-value map-options options-dense :options="cacheOptions" :label="t('activity.cacheLabel')" class="act-toolbar__filter" @update:model-value="(v) => set({ cache: v ?? null })" />
    <q-select :model-value="modelValue.outcome" dense outlined clearable emit-value map-options options-dense :options="outcomeOptions" :label="t('activity.outcomeLabel')" class="act-toolbar__filter" @update:model-value="(v) => set({ outcome: v ?? null })" />

    <q-btn outline no-caps class="act-toolbar__time" :icon="iconCalendar" :label="timeLabel" :aria-label="`${t('activity.rangeLabel')}: ${timeLabel}`">
      <q-menu anchor="bottom right" self="top right" @before-show="seedCustom">
        <q-list dense class="act-toolbar__menu">
          <q-item v-for="opt in rangeOptions" :key="opt.label" v-close-popup clickable :active="isRange(opt.value)" @click="set({ range: opt.value, from: null, to: null })">
            <q-item-section>{{ opt.label }}</q-item-section>
          </q-item>
          <q-separator />
          <q-item-label header>{{ t('activity.customRange') }}</q-item-label>
          <div class="act-toolbar__custom">
            <q-input v-model="customFrom" dense outlined type="datetime-local" step="1" :label="t('activity.from')" stack-label />
            <q-input v-model="customTo" dense outlined type="datetime-local" step="1" :label="t('activity.to')" stack-label />
            <q-btn v-close-popup unelevated no-caps color="primary" :label="t('activity.apply')" :disable="!customValid" @click="applyCustom" />
          </div>
        </q-list>
      </q-menu>
    </q-btn>

    <q-btn v-if="count" flat no-caps :icon="iconFilterOff" :label="t('activity.clear')" @click="emit('update:modelValue', { ...NO_FILTERS })" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import type { Format } from '@/api/types'
import { t } from '@/i18n'
import { ACTIVITY_CACHE_STATES, ACTIVITY_KINDS, ACTIVITY_OUTCOMES, ACTIVITY_RANGES, type ActivityFilters, type ActivityRange, activeFilterCount, kindMeta, NO_FILTERS } from '@/utils/activity'
import { formatLabel, LISTED_FORMATS } from '@/utils/domain'
import { absoluteTime } from '@/utils/format'
import { iconCalendar, iconFilterOff, iconPerson } from '@/utils/icons'

const props = defineProps<{ modelValue: ActivityFilters; repositories: string[] }>()
const emit = defineEmits<{ 'update:modelValue': [ActivityFilters] }>()

const principal = computed(() => props.modelValue.principal)
const count = computed(() => activeFilterCount(props.modelValue))

function set(change: Partial<ActivityFilters>): void {
  emit('update:modelValue', { ...props.modelValue, ...change })
}

const kindOptions = ACTIVITY_KINDS.map((k) => ({ label: kindMeta(k).label, value: k }))
const formatOptions = computed(() => {
  const formats: Format[] = [...LISTED_FORMATS]
  if (props.modelValue.format && !formats.includes(props.modelValue.format)) formats.push(props.modelValue.format)
  return formats.map((f) => ({ label: formatLabel(f), value: f }))
})
const cacheOptions = ACTIVITY_CACHE_STATES.map((c) => ({ label: c, value: c }))
const outcomeOptions = ACTIVITY_OUTCOMES.map((o) => ({ label: t(`activity.outcome.${o}`), value: o }))
const rangeOptions: { label: string; value: ActivityRange | null }[] = [
  { label: t('activity.anyTime'), value: null },
  ...(Object.keys(ACTIVITY_RANGES) as ActivityRange[]).map((r) => ({ label: t(`activity.range.${r}`), value: r })),
]

const repoNeedle = ref('')
const repoOptions = computed(() => {
  const names = [...props.repositories]
  if (props.modelValue.repo && !names.includes(props.modelValue.repo)) names.push(props.modelValue.repo)
  return names.map((n) => ({ label: n, value: n }))
})
const repoOptionsFiltered = computed(() => repoOptions.value.filter((o) => o.label.toLowerCase().includes(repoNeedle.value)))
function filterRepos(value: string, update: (fn: () => void) => void): void {
  update(() => {
    repoNeedle.value = value.toLowerCase()
  })
}

function isRange(value: ActivityRange | null): boolean {
  const f = props.modelValue
  return value === null ? !f.range && !f.from && !f.to : f.range === value
}

const timeLabel = computed(() => {
  const f = props.modelValue
  if (f.range) return t(`activity.range.${f.range}`)
  if (f.from || f.to) return `${f.from ? absoluteTime(f.from) : '…'} – ${f.to ? absoluteTime(f.to) : t('activity.following')}`
  return t('activity.anyTime')
})

// Custom range: datetime-local values (the user's timezone) <-> ISO.
const customFrom = ref('')
const customTo = ref('')
const pad = (n: number) => String(n).padStart(2, '0')
function toLocalInput(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
function fromLocalInput(value: string): string | null {
  if (!value) return null
  const ms = new Date(value).getTime()
  return Number.isNaN(ms) ? null : new Date(ms).toISOString()
}
function seedCustom(): void {
  const f = props.modelValue
  const since = f.range ? new Date(Date.now() - ACTIVITY_RANGES[f.range]).toISOString() : f.from
  customFrom.value = toLocalInput(since)
  customTo.value = toLocalInput(f.range ? null : f.to)
}
const customValid = computed(() => {
  const from = fromLocalInput(customFrom.value)
  const to = fromLocalInput(customTo.value)
  if (!from && !to) return false
  return !from || !to || from <= to
})
function applyCustom(): void {
  set({ range: null, from: fromLocalInput(customFrom.value), to: fromLocalInput(customTo.value) })
}
</script>

<style scoped>
.act-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.act-toolbar__user {
  flex: 1 1 200px;
  max-width: 280px;
}

.act-toolbar__filter {
  flex: 0 1 140px;
  min-width: 110px;
}

.act-toolbar__filter--wide {
  flex-basis: 180px;
}

.act-toolbar__time {
  height: 40px;
  border-radius: 8px;
  font-weight: 550;
}

.act-toolbar__time::before {
  border-color: var(--border-strong);
}

.act-toolbar__menu {
  min-width: 280px;
  padding: 4px 0 8px;
}

.act-toolbar__custom {
  display: grid;
  gap: 8px;
  padding: 0 12px;
}

@media (max-width: 599px) {
  .act-toolbar__user,
  .act-toolbar__filter {
    flex: 1 1 calc(50% - 4px);
    max-width: none;
  }

  .act-toolbar__time {
    flex: 1 1 100%;
  }
}
</style>
