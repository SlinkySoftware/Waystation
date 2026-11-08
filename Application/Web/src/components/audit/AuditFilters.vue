<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Audit log toolbar: time range (or custom dates), actor, actions (categories or single actions), target and
     outcome. Every value is kept in the URL by useAuditFilters. -->
<template>
  <div class="audit-filters">
    <q-select
      :model-value="state.range"
      dense
      outlined
      emit-value
      map-options
      options-dense
      :options="rangeOptions"
      :label="t('audit.range')"
      class="audit-filters__range"
      @update:model-value="(v: AuditRange) => update({ range: v })"
    />
    <template v-if="state.range === 'custom'">
      <q-input :model-value="state.from" dense outlined type="date" stack-label :label="t('audit.from')" class="audit-filters__date" @update:model-value="(v) => update({ from: str(v) })" />
      <q-input :model-value="state.to" dense outlined type="date" stack-label :label="t('audit.to')" class="audit-filters__date" @update:model-value="(v) => update({ to: str(v) })" />
    </template>
    <q-input :model-value="state.actor" dense outlined clearable debounce="300" :label="t('audit.actor')" :placeholder="t('audit.actorHint')" class="audit-filters__text" @update:model-value="(v) => update({ actor: str(v) })" />
    <q-select
      :model-value="state.actions"
      dense
      outlined
      multiple
      emit-value
      map-options
      options-dense
      use-chips
      clearable
      :options="actionOptions"
      :label="t('audit.actions')"
      class="audit-filters__actions"
      @update:model-value="(v: string[] | null) => update({ actions: v ?? [] })"
    >
      <template #option="{ opt, itemProps, selected, toggleOption }">
        <q-item v-bind="itemProps" :class="opt.category ? 'audit-filters__cat' : 'audit-filters__act'" @click="toggleOption(opt)">
          <q-item-section side><q-checkbox :model-value="selected" dense size="sm" @update:model-value="toggleOption(opt)" /></q-item-section>
          <q-item-section v-if="opt.category" side><q-icon :name="opt.icon" size="18px" /></q-item-section>
          <q-item-section>{{ opt.label }}</q-item-section>
        </q-item>
      </template>
    </q-select>
    <q-input :model-value="state.target" dense outlined clearable debounce="300" :label="t('audit.target')" :placeholder="t('audit.targetHint')" class="audit-filters__text" @update:model-value="(v) => update({ target: str(v) })" />
    <q-select
      :model-value="state.outcome"
      dense
      outlined
      clearable
      emit-value
      map-options
      options-dense
      :options="outcomeOptions"
      :label="t('audit.outcome')"
      class="audit-filters__outcome"
      @update:model-value="(v: AuditOutcome | null) => update({ outcome: v })"
    />
  </div>
</template>

<script setup lang="ts">
import type { AuditOutcome } from '@/api/types'
import { AUDIT_RANGES, type AuditRange, useAuditFilters } from '@/composables/useAuditFilters'
import { t } from '@/i18n'
import { AUDIT_ACTIONS, AUDIT_CATEGORIES, auditActionLabel, auditCategoryIcon, auditCategoryLabel } from '@/utils/audit'

const { state, update } = useAuditFilters()

const str = (v: string | number | null | undefined): string | null => (v === null || v === undefined || v === '' ? null : String(v))

const rangeOptions = AUDIT_RANGES.map((r) => ({ value: r, label: t(`audit.ranges.${r}`) }))
const outcomeOptions = (['success', 'failure'] as const).map((o) => ({ value: o, label: t(`audit.outcomes.${o}`) }))

interface ActionOption {
  value: string
  label: string
  category: boolean
  icon: string
}

// A category wildcard (`token.*`) followed by its actions.
const actionOptions: ActionOption[] = AUDIT_CATEGORIES.flatMap((c) => [
  { value: `${c}.*`, label: t('audit.allInCategory', { category: auditCategoryLabel(c) }), category: true, icon: auditCategoryIcon(`${c}.x`) },
  ...AUDIT_ACTIONS[c].map((v) => ({ value: `${c}.${v}`, label: auditActionLabel(`${c}.${v}`), category: false, icon: '' })),
])
</script>

<style scoped>
.audit-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.audit-filters__range {
  flex: 0 1 170px;
  min-width: 140px;
}

.audit-filters__date {
  flex: 0 1 160px;
}

.audit-filters__text {
  flex: 1 1 150px;
  max-width: 220px;
}

.audit-filters__actions {
  flex: 1 1 220px;
  max-width: 360px;
}

.audit-filters__outcome {
  flex: 0 1 140px;
  min-width: 120px;
}

.audit-filters__cat {
  font-weight: 600;
}

.audit-filters__act {
  padding-left: 40px;
}

@media (max-width: 599px) {
  .audit-filters__text,
  .audit-filters__actions,
  .audit-filters__range,
  .audit-filters__outcome {
    flex: 1 1 100%;
    max-width: none;
  }
}
</style>
