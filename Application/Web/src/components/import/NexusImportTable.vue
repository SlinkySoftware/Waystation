<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Import from Nexus, step 2: one row per Nexus repository with what the import does (or did) to it. Rows the
     import can create carry a checkbox; any row expands to its reason, field-level differences and notes. -->
<template>
  <q-table
    class="ws-table nx-table"
    flat
    :rows="items"
    :columns="columns"
    row-key="name"
    :pagination="{ rowsPerPage: 0 }"
    hide-pagination
    :grid="$q.screen.lt.sm"
    :no-data-label="t('nexusImport.noRepositories')"
  >
    <template #header-cell-select>
      <q-th class="nx-table__check">
        <q-checkbox
          v-if="!applied"
          dense
          :model-value="allState"
          :disable="!creatable.length"
          :aria-label="t('nexusImport.selectAll')"
          @update:model-value="toggleAll"
        />
      </q-th>
    </template>
    <template #body="scope">
      <q-tr :props="scope" :class="{ 'nx-table__row': detailCount(scope.row) }" @click="detailCount(scope.row) && toggleOpen(scope.row.name)">
        <q-td class="nx-table__check" @click.stop>
          <q-checkbox
            v-if="scope.row.action === 'create' && !applied"
            dense
            :model-value="selected.includes(scope.row.name)"
            :aria-label="t('nexusImport.selectOne', { name: scope.row.name })"
            @update:model-value="(v: boolean) => toggle(scope.row.name, v)"
          />
        </q-td>
        <q-td>
          <div class="nx-table__name">
            <FormatIcon v-if="scope.row.format" :mark="formatMark(scope.row.format, upstreamOf(scope.row))" :size="18" brand />
            <q-icon v-else :name="iconPackage" size="18px" class="text-faint" />
            <span class="nx-table__repo">{{ scope.row.name }}</span>
          </div>
        </q-td>
        <q-td class="mono nx-table__kind">{{ scope.row.nexus_format }} {{ scope.row.nexus_type }}</q-td>
        <q-td class="mono nx-table__kind gt-sm">{{ waystationKind(scope.row) }}</q-td>
        <q-td class="nx-table__target gt-sm"><span class="mono">{{ target(scope.row) }}</span></q-td>
        <q-td>
          <span class="ws-badge" :class="rowState(scope.row).cls"><q-icon :name="rowState(scope.row).icon" />{{ rowState(scope.row).label }}</span>
          <div v-if="scope.row.reason" class="nx-table__reason">{{ scope.row.reason }}</div>
        </q-td>
        <q-td class="text-right">
          <q-btn
            v-if="detailCount(scope.row)"
            flat
            dense
            no-caps
            size="sm"
            class="nx-table__more"
            :icon-right="open.has(scope.row.name) ? iconUp : iconDown"
            :label="t('nexusImport.differences', { count: detailCount(scope.row) })"
            :aria-expanded="open.has(scope.row.name)"
            @click.stop="toggleOpen(scope.row.name)"
          />
        </q-td>
      </q-tr>
      <q-tr v-if="open.has(scope.row.name)" :props="scope" class="nx-table__detail-row">
        <q-td colspan="100%">
          <ImportItemDetail :item="scope.row" />
        </q-td>
      </q-tr>
    </template>
    <template #item="{ row }">
      <div class="nx-card">
        <div class="nx-table__name">
          <q-checkbox v-if="row.action === 'create' && !applied" dense :model-value="selected.includes(row.name)" :aria-label="t('nexusImport.selectOne', { name: row.name })" @update:model-value="(v: boolean) => toggle(row.name, v)" />
          <span class="nx-table__repo">{{ row.name }}</span>
          <span class="ws-badge" :class="rowState(row).cls"><q-icon :name="rowState(row).icon" />{{ rowState(row).label }}</span>
        </div>
        <div class="mono nx-table__kind">{{ row.nexus_format }} {{ row.nexus_type }} → {{ waystationKind(row) }}</div>
        <div v-if="row.reason" class="nx-table__reason">{{ row.reason }}</div>
        <ImportItemDetail v-if="row.differences.length || row.notes.length" :item="row" compact />
      </div>
    </template>
  </q-table>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref } from 'vue'

import type { NexusImportItem } from '@/api/types'
import FormatIcon from '@/components/FormatIcon.vue'
import { t } from '@/i18n'
import { formatMark } from '@/utils/domain'
import { iconDown, iconPackage, iconUp } from '@/utils/icons'

import ImportItemDetail from './ImportItemDetail.vue'
import { rowState, target, waystationKind } from './nexusImport'

const props = defineProps<{ items: NexusImportItem[]; applied: boolean }>()
const selected = defineModel<string[]>('selected', { required: true })

const open = ref(new Set<string>())
const creatable = computed(() => props.items.filter((i) => i.action === 'create').map((i) => i.name))
const allState = computed(() => (selected.value.length === 0 ? false : selected.value.length === creatable.value.length ? true : null))

const columns = computed<QTableColumn<NexusImportItem>[]>(() => [
  { name: 'select', label: '', field: 'name', align: 'left' },
  { name: 'name', label: t('nexusImport.col.name'), field: 'name', align: 'left' },
  { name: 'nexus', label: t('nexusImport.col.nexus'), field: 'nexus_format', align: 'left' },
  { name: 'waystation', label: t('nexusImport.col.waystation'), field: 'format', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'target', label: t('nexusImport.col.target'), field: 'name', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'state', label: t('nexusImport.col.state'), field: 'action', align: 'left' },
  { name: 'details', label: '', field: 'name', align: 'right' },
])

function detailCount(item: NexusImportItem): number {
  return item.differences.length + item.notes.length
}

function upstreamOf(item: NexusImportItem): string | null {
  return item.repository?.proxy?.upstream_url ?? null
}

function toggle(name: string, on: boolean): void {
  selected.value = on ? [...new Set([...selected.value, name])] : selected.value.filter((n) => n !== name)
}

function toggleAll(on: boolean | null): void {
  selected.value = on ? [...creatable.value] : []
}

function toggleOpen(name: string): void {
  const next = new Set(open.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  open.value = next
}
</script>

<style scoped>
.nx-table__check {
  width: 40px;
}
.nx-table__row {
  cursor: pointer;
}
.nx-table__more {
  white-space: nowrap;
}
.nx-table__more :deep(.q-btn__content) {
  flex-wrap: nowrap;
}
.nx-table__name {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.nx-table__repo {
  font-weight: 600;
}
.nx-table__kind {
  color: var(--text-secondary);
  font-size: var(--fs-13);
  white-space: nowrap;
}
.nx-table__target {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
  font-size: var(--fs-13);
}
.nx-table__reason {
  margin-top: 4px;
  max-width: 420px;
  white-space: normal;
  color: var(--text-secondary);
  font-size: var(--fs-12);
}
.nx-table__detail-row > td {
  background: var(--surface-subtle);
}
.nx-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
.nx-card .ws-badge {
  margin-left: auto;
}
</style>
