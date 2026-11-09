<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Dead letters (UI_DESIGN §4.9): jobs that failed every attempt, newest first: job, last error, attempts, when it
     gave up; retry (queued again with fresh attempts) or discard (confirmed). The payload is shown on expand.
     Malformed entries can only be discarded. Cards on phones. -->
<template>
  <PageCard :title="t('tasks.dead.title')" :caption="t('tasks.dead.caption')" flush>
    <template #actions>
      <span v-if="page && page.total" class="dead__total">{{ t('tasks.dead.total', { count: page.total, n: formatCount(page.total) }) }}</span>
    </template>
    <q-table class="ws-table dead" flat :rows="rows" :columns="columns" row-key="id" :loading="loading && !rows.length" :grid="$q.screen.lt.sm" :pagination="{ rowsPerPage: 0 }" hide-pagination>
      <template #body="props">
        <q-tr :props="props">
          <q-td key="job" :props="props">
            <div class="dead__name">
              <q-btn flat round dense size="sm" :icon="iconExpand" :class="{ 'dead__chevron--open': props.expand }" :aria-label="t('tasks.dead.payload')" :aria-expanded="props.expand" @click="props.expand = !props.expand" />
              {{ props.row.kind ? kindLabel(props.row.kind) : t('tasks.dead.malformed') }}
            </div>
            <div class="dead__sub mono">{{ props.row.job_id ?? props.row.id }}</div>
          </q-td>
          <q-td key="error" :props="props" class="dead__error">
            {{ props.row.error }}<q-tooltip max-width="420px">{{ props.row.error }}</q-tooltip>
          </q-td>
          <q-td key="attempts" :props="props" class="num">{{ props.row.attempts }}</q-td>
          <q-td key="dead" :props="props"><TimeCell :value="props.row.dead_at" /></q-td>
          <q-td key="actions" :props="props" class="dead__actions">
            <DeadLetterActions :letter="props.row" :busy="busy === props.row.id" @retry="emit('retry', props.row)" @discard="emit('discard', props.row)" />
          </q-td>
        </q-tr>
        <q-tr v-if="props.expand" :props="props" class="dead__expanded">
          <q-td colspan="100%">
            <div v-if="!props.row.retryable" class="dead__note">{{ t('tasks.dead.malformedHelp') }}</div>
            <pre class="dead__payload mono">{{ props.row.payload ? JSON.stringify(props.row.payload, null, 2) : '–' }}</pre>
          </q-td>
        </q-tr>
      </template>

      <template #item="{ row }">
        <div class="dead__card">
          <div class="dead__name">{{ row.kind ? kindLabel(row.kind) : t('tasks.dead.malformed') }}</div>
          <div class="dead__error dead__error--card">{{ row.error }}</div>
          <div class="dead__sub"><TimeCell :value="row.dead_at" /> · {{ t('tasks.dead.col.attempts') }} {{ row.attempts }}</div>
          <DeadLetterActions :letter="row" :busy="busy === row.id" @retry="emit('retry', row)" @discard="emit('discard', row)" />
        </div>
      </template>

      <template #no-data>
        <EmptyState class="full-width" compact illustration="cache" :title="t('tasks.dead.emptyTitle')" :text="t('tasks.dead.emptyText')" />
      </template>
    </q-table>
    <template v-if="page?.next_cursor" #footer>
      <q-btn flat no-caps color="primary" class="full-width" :label="t('tasks.runs.loadMore')" :loading="loading" @click="emit('more')" />
    </template>
  </PageCard>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed } from 'vue'

import type { DeadLetter, DeadLetterPage } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import TimeCell from '@/components/TimeCell.vue'
import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconExpand } from '@/utils/icons'

import DeadLetterActions from './DeadLetterActions.vue'
import { kindLabel } from './tasks'

defineProps<{ page: DeadLetterPage | null; rows: DeadLetter[]; loading: boolean; busy: string | null }>()
const emit = defineEmits<{ retry: [letter: DeadLetter]; discard: [letter: DeadLetter]; more: [] }>()
const $q = useQuasar()

const columns = computed<QTableColumn<DeadLetter>[]>(() => [
  { name: 'job', label: t('tasks.dead.col.job'), field: (r) => r.kind ?? '', align: 'left' },
  { name: 'error', label: t('tasks.dead.col.error'), field: 'error', align: 'left' },
  { name: 'attempts', label: t('tasks.dead.col.attempts'), field: 'attempts', align: 'right' },
  { name: 'dead', label: t('tasks.dead.col.dead'), field: 'dead_at', align: 'left' },
  { name: 'actions', label: '', field: 'id', align: 'right' },
])
</script>

<style scoped>
.dead__total {
  font-size: var(--fs-13);
  color: var(--status-blocked);
  font-weight: 600;
}

.dead__name {
  display: flex;
  align-items: center;
  gap: 4px;
  font-weight: 600;
}

.dead__chevron--open {
  transform: rotate(180deg);
}

.dead__sub {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  padding-left: 32px;
}

.dead__error {
  max-width: 420px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--status-blocked);
  font-size: var(--fs-13);
}

.dead__error--card {
  max-width: none;
  white-space: normal;
  overflow-wrap: anywhere;
}

.dead__actions {
  white-space: nowrap;
}

.dead__expanded :deep(td) {
  background: var(--surface-subtle);
}

.dead__note {
  margin-bottom: 6px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.dead__payload {
  margin: 0;
  font-size: var(--fs-12);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.dead__card {
  width: 100%;
  display: grid;
  gap: 6px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.dead__card .dead__sub {
  padding-left: 0;
}
</style>
