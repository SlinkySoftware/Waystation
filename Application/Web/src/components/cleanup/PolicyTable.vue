<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Cleanup policies table: name, format, criteria chips, schedule, attached repositories, last run (result and
     bytes), next run, enabled; row actions. Cards on phones. -->
<template>
  <q-table
    class="ws-table policies"
    flat
    :rows="policies"
    :columns="columns"
    row-key="id"
    :loading="loading"
    :grid="$q.screen.lt.sm"
    :pagination="{ rowsPerPage: 0 }"
    hide-pagination
    @row-click="(_e: Event, row: CleanupPolicy) => canEdit && emit('edit', row)"
  >
    <template #body-cell-name="{ row }">
      <q-td :class="{ 'cursor-pointer': canEdit, 'policies--off': !row.enabled }">
        <div class="policies__title">
          <PolicyFormat :format="row.format" icon-only />
          <div>
            <div class="policies__name">{{ row.name }}</div>
            <div class="policies__desc">{{ formatScope(row.format) }}<template v-if="row.description"> · {{ row.description }}</template></div>
          </div>
        </div>
      </q-td>
    </template>
    <template #body-cell-criteria="{ row }">
      <q-td class="policies__criteria"><CriteriaChips :criteria="row.criteria" /></q-td>
    </template>
    <template #body-cell-repositories="{ row }">
      <q-td @click.stop>
        <span v-if="!row.repositories.length" class="text-faint">{{ t('cleanup.notAttached') }}</span>
        <span v-else class="policies__repos">
          <RepoChip v-for="r in row.repositories.slice(0, 3)" :key="r" :name="r" :format="formatOf(r)" />
          <span v-if="row.repositories.length > 3" class="policies__more">{{ t('cleanup.moreRepos', { count: row.repositories.length - 3 }) }}<q-tooltip>{{ row.repositories.slice(3).join(', ') }}</q-tooltip></span>
        </span>
      </q-td>
    </template>
    <template #body-cell-lastRun="{ row }">
      <q-td><LastRun :run="row.last_run" /></q-td>
    </template>
    <template #body-cell-nextRun="{ row }">
      <q-td class="policies__schedule">
        <TimeCell v-if="row.next_run_at" :value="row.next_run_at" class="policies__next" />
        <span v-else class="text-faint policies__next">{{ row.schedule ? t('cleanup.paused') : t('cleanup.manual') }}</span>
        <div v-if="row.schedule" class="policies__desc">{{ describeSchedule(row.schedule) }}</div>
      </q-td>
    </template>
    <template #body-cell-enabled="{ row }">
      <q-td @click.stop>
        <q-toggle :model-value="row.enabled" dense :disable="!canEdit" :aria-label="t('cleanup.action.enableWhat', { name: row.name })" @update:model-value="(on: boolean) => emit('toggle', row, on)" />
      </q-td>
    </template>
    <template #body-cell-actions="{ row }">
      <q-td class="text-right policies__actions" @click.stop>
        <PolicyActions :policy="row" :can-edit="canEdit" @run="emit('run', row)" @history="emit('history', row)" @edit="emit('edit', row)" @delete="emit('delete', row)" />
      </q-td>
    </template>

    <template #item="{ row }">
      <div class="policies__card" :class="{ 'policies--off': !row.enabled }">
        <div class="policies__card-head">
          <PolicyFormat :format="row.format" icon-only />
          <div class="col">
            <div class="policies__name">{{ row.name }}</div>
            <div class="policies__desc">{{ describeSchedule(row.schedule) }}</div>
          </div>
          <q-toggle :model-value="row.enabled" dense :disable="!canEdit" :aria-label="t('cleanup.action.enableWhat', { name: row.name })" @update:model-value="(on: boolean) => emit('toggle', row, on)" />
        </div>
        <CriteriaChips :criteria="row.criteria" />
        <LastRun :run="row.last_run" />
        <PolicyActions :policy="row" :can-edit="canEdit" @run="emit('run', row)" @history="emit('history', row)" @edit="emit('edit', row)" @delete="emit('delete', row)" />
      </div>
    </template>

    <template #no-data>
      <slot name="empty" />
    </template>
  </q-table>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed } from 'vue'

import type { CleanupPolicy, Format } from '@/api/types'
import RepoChip from '@/components/RepoChip.vue'
import TimeCell from '@/components/TimeCell.vue'
import { t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'

import { describeSchedule, formatScope } from './cleanup'
import CriteriaChips from './CriteriaChips.vue'
import LastRun from './LastRun.vue'
import PolicyActions from './PolicyActions.vue'
import PolicyFormat from './PolicyFormat.vue'

defineProps<{ policies: CleanupPolicy[]; loading: boolean; canEdit: boolean }>()
const emit = defineEmits<{ edit: [policy: CleanupPolicy]; run: [policy: CleanupPolicy]; history: [policy: CleanupPolicy]; delete: [policy: CleanupPolicy]; toggle: [policy: CleanupPolicy, enabled: boolean] }>()
const $q = useQuasar()
const repos = useRepositoriesStore()
const formatOf = (name: string): Format | null => repos.byName.get(name)?.format ?? null

const columns = computed<QTableColumn<CleanupPolicy>[]>(() => [
  { name: 'name', label: t('cleanup.col.name'), field: 'name', align: 'left' },
  { name: 'criteria', label: t('cleanup.col.criteria'), field: (r) => r.id, align: 'left' },
  { name: 'repositories', label: t('cleanup.col.repositories'), field: (r) => r.repositories.length, align: 'left' },
  { name: 'lastRun', label: t('cleanup.col.lastRun'), field: (r) => r.last_run?.started_at ?? '', align: 'left' },
  { name: 'nextRun', label: t('cleanup.col.schedule'), field: (r) => r.next_run_at ?? '', align: 'left' },
  { name: 'enabled', label: t('cleanup.col.enabled'), field: 'enabled', align: 'left' },
  { name: 'actions', label: '', field: 'id', align: 'right' },
])
</script>

<style scoped>
.policies__title {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.policies__name {
  font-weight: 600;
}

.policies__desc {
  max-width: 190px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  white-space: normal;
}

.policies--off .policies__name {
  color: var(--text-secondary);
}

.policies__criteria {
  min-width: 140px;
  max-width: 240px;
  white-space: normal;
}

.policies__schedule {
  font-size: var(--fs-13);
  white-space: normal;
  min-width: 120px;
}

.policies__repos {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  max-width: 200px;
}

.policies__more {
  align-self: center;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.policies__actions {
  white-space: nowrap;
  padding-left: 0 !important;
  padding-right: 8px !important;
}

.policies__card {
  width: 100%;
  display: grid;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.policies__card-head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
</style>
