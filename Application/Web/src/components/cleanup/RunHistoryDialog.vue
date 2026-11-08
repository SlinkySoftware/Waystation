<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A policy's run history: status, trigger, start, duration, what was removed, errors; running runs show a
     progress bar and the list refreshes every 2 s until they finish. -->
<template>
  <FormDialog v-model="open" readonly :title="t('cleanup.history.title')" :caption="policy ? t('cleanup.history.caption', { name: policy.name }) : undefined" width="920px">
    <div v-if="list.error.value && !list.data.value" class="history__error" role="alert">
      {{ errorMessage(list.error.value) }}
      <q-btn flat dense no-caps :label="t('common.retry')" @click="list.reload" />
    </div>
    <q-table
      v-else
      class="ws-table history"
      flat
      dense
      :rows="list.data.value?.items ?? []"
      :columns="columns"
      row-key="id"
      :loading="list.loading.value && !list.data.value"
      :grid="$q.screen.lt.sm"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
    >
      <template #body-cell-status="{ row }">
        <q-td><RunStatusPill :status="row.status" /></q-td>
      </template>
      <template #body-cell-started="{ row }">
        <q-td><TimeCell :value="row.started_at" /></q-td>
      </template>
      <template #body-cell-trigger="{ row }">
        <q-td>{{ row.trigger === 'manual' ? t('cleanup.run.manual') : t('cleanup.run.schedule') }}</q-td>
      </template>
      <template #body-cell-duration="{ row }">
        <q-td class="num">{{ formatDuration(runSeconds(row, now)) }}</q-td>
      </template>
      <template #body-cell-removed="{ row }">
        <q-td class="num text-right">{{ formatCount(row.removed) }}</q-td>
      </template>
      <template #body-cell-bytes="{ row }">
        <q-td class="text-right"><SizeCell :bytes="row.bytes" /></q-td>
      </template>
      <template #body-cell-detail="{ row }">
        <q-td class="history__detail"><RunDetail :run="row" /></q-td>
      </template>

      <template #item="{ row }">
        <div class="history__card">
          <div class="history__card-head">
            <RunStatusPill :status="row.status" :tooltip="false" />
            <TimeCell :value="row.started_at" />
          </div>
          <div class="history__card-meta">
            {{ formatCount(row.removed) }} · <SizeCell :bytes="row.bytes" /> · {{ formatDuration(runSeconds(row, now)) }}
          </div>
          <RunDetail :run="row" />
        </div>
      </template>

      <template #no-data>
        <EmptyState class="full-width" compact illustration="activity" :title="t('cleanup.history.empty')" :text="t('cleanup.history.emptyText')" />
      </template>
    </q-table>
  </FormDialog>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { CleanupPolicy, CleanupRunList, CleanupRunSummary } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import FormDialog from '@/components/FormDialog.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNow } from '@/composables/useNow'
import { usePolling } from '@/composables/usePolling'
import { t } from '@/i18n'
import { formatCount, formatDuration } from '@/utils/format'

import { runSeconds } from './cleanup'
import RunDetail from './RunDetail.vue'
import RunStatusPill from './RunStatusPill.vue'

const props = defineProps<{ policy: CleanupPolicy | null }>()
const open = defineModel<boolean>({ required: true })
const $q = useQuasar()
const now = useNow()

const list = useAsync<CleanupRunList | null>(async () => (open.value && props.policy ? call(api.GET('/cleanup-policies/{policy_id}/runs', { params: { path: { policy_id: props.policy.id } } })) : null), [open, () => props.policy?.id])
const running = computed(() => open.value && (list.data.value?.items ?? []).some((r) => r.status === 'running'))
usePolling(running, () => list.reload())

const columns = computed<QTableColumn<CleanupRunSummary>[]>(() => [
  { name: 'status', label: t('cleanup.history.col.status'), field: 'status', align: 'left' },
  { name: 'started', label: t('cleanup.history.col.started'), field: 'started_at', align: 'left' },
  { name: 'trigger', label: t('cleanup.history.col.trigger'), field: 'trigger', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'duration', label: t('cleanup.history.col.duration'), field: (r) => r.finished_at ?? '', align: 'left' },
  { name: 'removed', label: t('cleanup.history.col.removed'), field: 'removed', align: 'right' },
  { name: 'bytes', label: t('cleanup.history.col.bytes'), field: 'bytes', align: 'right' },
  { name: 'detail', label: t('cleanup.history.col.detail'), field: (r) => r.error ?? '', align: 'left' },
])
</script>

<style scoped>
.history__error {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--cache-error);
}

.history__detail {
  min-width: 200px;
  max-width: 340px;
  white-space: normal;
}

.history__card {
  width: 100%;
  display: grid;
  gap: 6px;
  padding: 12px 4px;
  border-bottom: 1px solid var(--border);
}

.history__card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.history__card-meta {
  font-size: var(--fs-13);
  color: var(--text-secondary);
}
</style>
