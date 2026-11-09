<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Criteria builder: each criterion reads as a sentence with a switch; numbers edit inline. Selecting criteria,
     protections and name/version patterns are grouped. The docker-only criterion shows for docker and `*`. -->
<template>
  <div class="criteria">
    <div v-if="errors.criteria" class="criteria__error" role="alert">{{ errors.criteria }}</div>

    <div class="criteria__group-label">{{ t('cleanup.editor.selectors') }}</div>
    <div v-for="row in selectRows" :key="row.key" class="crit" :class="{ 'crit--on': isOn(row.key) }">
      <q-toggle :model-value="isOn(row.key)" dense :aria-label="row.before" @update:model-value="(on: boolean) => toggle(row.key, on)" />
      <div class="crit__body">
        <div class="crit__sentence">
          <span>{{ row.before }}</span>
          <q-input
            v-if="row.number && isOn(row.key)"
            :model-value="model[row.key as NumberCriterion]"
            type="number"
            dense
            outlined
            hide-bottom-space
            class="crit__num"
            :min="1"
            :max="NUMBER_LIMITS[row.key as NumberCriterion].max"
            :aria-label="row.before"
            :rules="[(v: number | string) => inRange(v, NUMBER_LIMITS[row.key as NumberCriterion].max) || t('cleanup.editor.range', { max: NUMBER_LIMITS[row.key as NumberCriterion].max })]"
            :error="Boolean(criteriaError(errors, row.key))"
            :error-message="criteriaError(errors, row.key)"
            @update:model-value="(v: string | number | null) => setNumber(row.key as NumberCriterion, v)"
          />
          <span v-else-if="row.number" class="crit__blank">N</span>
          <span>{{ row.after }}</span>
        </div>
        <div class="crit__help">{{ row.help }}</div>
        <div v-if="!row.number && criteriaError(errors, row.key)" class="criteria__field-error">{{ criteriaError(errors, row.key) }}</div>
      </div>
    </div>

    <div class="criteria__group-label">{{ t('cleanup.editor.protections') }}</div>
    <div class="crit" :class="{ 'crit--on': isOn('keep_latest_n_versions') }">
      <q-toggle :model-value="isOn('keep_latest_n_versions')" dense :aria-label="keepRow.before" @update:model-value="(on: boolean) => toggle('keep_latest_n_versions', on)" />
      <div class="crit__body">
        <div class="crit__sentence">
          <span>{{ keepRow.before }}</span>
          <q-input
            v-if="isOn('keep_latest_n_versions')"
            :model-value="model.keep_latest_n_versions"
            type="number"
            dense
            outlined
            hide-bottom-space
            class="crit__num"
            :min="1"
            :max="1000"
            :aria-label="keepRow.before"
            :rules="[(v: number | string) => inRange(v, 1000) || t('cleanup.editor.range', { max: 1000 })]"
            :error="Boolean(criteriaError(errors, 'keep_latest_n_versions'))"
            :error-message="criteriaError(errors, 'keep_latest_n_versions')"
            @update:model-value="(v: string | number | null) => setNumber('keep_latest_n_versions', v)"
          />
          <span v-else class="crit__blank">N</span>
          <span>{{ keepRow.after }}</span>
        </div>
        <div class="crit__help">{{ keepRow.help }}</div>
      </div>
    </div>

    <div class="criteria__group-label">
      {{ t('cleanup.editor.patterns') }}
      <span class="criteria__group-caption">{{ t('cleanup.editor.patternsCaption') }}</span>
    </div>
    <div class="criteria__patterns">
      <q-input
        v-for="key in REGEX_CRITERIA"
        :key="key"
        :model-value="model[key] ?? ''"
        dense
        outlined
        clearable
        class="mono"
        :label="t(`cleanup.criterion.${key}`)"
        :rules="[(v: string | null) => isValidRegex(v) || t('cleanup.editor.badRegex')]"
        :error="Boolean(criteriaError(errors, key))"
        :error-message="criteriaError(errors, key)"
        @update:model-value="(v: string | number | null) => setPattern(key, v)"
      >
        <template #prepend><q-icon :name="key.endsWith('exclude') ? iconLock : iconFilter" size="16px" /></template>
      </q-input>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { CleanupCriteria, CleanupFormat } from '@/api/types'
import { t } from '@/i18n'
import { iconFilter, iconLock } from '@/utils/icons'

import { criteriaError, isValidRegex, NUMBER_LIMITS, type NumberCriterion, REGEX_CRITERIA, type RegexCriterion, supportsUntagged } from './cleanup'

const props = withDefaults(defineProps<{ format: CleanupFormat; errors?: Record<string, string> }>(), { errors: () => ({}) })
const model = defineModel<CleanupCriteria>({ required: true })

type RowKey = NumberCriterion | 'prerelease_only'
interface Row {
  key: RowKey
  number: boolean
  before: string
  after: string
  help: string
}

const days = computed(() => t('cleanup.editor.days'))
const selectRows = computed<Row[]>(() => [
  { key: 'last_downloaded_older_than_days', number: true, before: t('cleanup.criterion.last_downloaded_older_than_days.before'), after: days.value, help: t('cleanup.criterion.last_downloaded_older_than_days.help') },
  { key: 'published_older_than_days', number: true, before: t('cleanup.criterion.published_older_than_days.before'), after: `${days.value} ${t('cleanup.criterion.published_older_than_days.after')}`, help: t('cleanup.criterion.published_older_than_days.help') },
  ...(supportsUntagged(props.format)
    ? [{ key: 'untagged_older_than_days' as const, number: true, before: t('cleanup.criterion.untagged_older_than_days.before'), after: days.value, help: t('cleanup.criterion.untagged_older_than_days.help') }]
    : []),
  { key: 'prerelease_only', number: false, before: t('cleanup.criterion.prerelease_only.label'), after: '', help: t('cleanup.criterion.prerelease_only.help') },
])
const keepRow = computed(() => ({
  before: t('cleanup.criterion.keep_latest_n_versions.before'),
  after: `${props.format === 'docker' ? t('cleanup.editor.tags') : t('cleanup.editor.versions')} ${t('cleanup.criterion.keep_latest_n_versions.after')}`,
  help: t('cleanup.criterion.keep_latest_n_versions.help'),
}))

function isOn(key: RowKey): boolean {
  return key === 'prerelease_only' ? Boolean(model.value.prerelease_only) : model.value[key] != null
}

function toggle(key: RowKey, on: boolean): void {
  if (key === 'prerelease_only') model.value = { ...model.value, prerelease_only: on }
  else model.value = { ...model.value, [key]: on ? NUMBER_LIMITS[key].initial : null }
}

function setNumber(key: NumberCriterion, value: string | number | null): void {
  const n = value === null || value === '' ? Number.NaN : Number(value)
  // Keep the switch on while the field is being retyped; an empty field fails its rule rather than switching off.
  model.value = { ...model.value, [key]: Number.isFinite(n) ? n : 0 }
}

function setPattern(key: RegexCriterion, value: string | number | null): void {
  model.value = { ...model.value, [key]: value ? String(value) : null }
}

function inRange(value: number | string, max: number): boolean {
  const n = Number(value)
  return Number.isInteger(n) && n >= 1 && n <= max
}
</script>

<style scoped>
.criteria {
  display: grid;
  gap: 8px;
}

.criteria__group-label {
  margin-top: 6px;
  font-size: var(--fs-12);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-secondary);
}

.criteria__group-caption {
  margin-left: 6px;
  font-weight: 400;
  text-transform: none;
  letter-spacing: 0;
}

.crit {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  transition: border-color var(--dur) var(--ease-out), background var(--dur) var(--ease-out);
}

.crit--on {
  border-color: color-mix(in srgb, var(--q-primary) 55%, var(--border));
  background: var(--surface-brand);
}

.crit__body {
  flex: 1;
  min-width: 0;
}

.crit__sentence {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 32px;
  font-weight: 550;
}

.crit__num {
  width: 92px;
}

.crit__num :deep(input) {
  font-variant-numeric: tabular-nums;
}

.crit__blank {
  display: inline-grid;
  place-items: center;
  min-width: 36px;
  height: 24px;
  border: 1px dashed var(--border);
  border-radius: 6px;
  color: var(--text-tertiary);
  font-size: var(--fs-12);
}

.crit__help {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.criteria__patterns {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 12px;
}

.criteria__error,
.criteria__field-error {
  font-size: var(--fs-13);
  color: var(--status-blocked);
}

.criteria__error {
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--status-blocked-bg);
}

@media (max-width: 599px) {
  .criteria__patterns {
    grid-template-columns: 1fr;
  }
}
</style>
