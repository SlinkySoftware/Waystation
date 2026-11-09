<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Webhooks table: name and target URL, events, repository scope, last delivery (status, result, when), on/off;
     row actions (send test event, deliveries, edit, delete). Cards on phones. -->
<template>
  <q-table
    class="ws-table hooks"
    flat
    :rows="webhooks"
    :columns="columns"
    row-key="id"
    :loading="loading"
    :grid="$q.screen.lt.sm"
    :pagination="{ rowsPerPage: 0 }"
    hide-pagination
    @row-click="(_e: Event, row: Webhook) => emit('deliveries', row)"
  >
    <template #body-cell-name="{ row }">
      <q-td class="cursor-pointer" :class="{ 'hooks--off': !row.enabled }">
        <div class="hooks__title">
          <div class="hooks__icon"><q-icon :name="iconWebhook" size="18px" /></div>
          <div class="hooks__ident">
            <div class="hooks__name">{{ row.name }}</div>
            <div class="hooks__url mono" :title="row.url">{{ row.url }}</div>
            <div v-if="row.description" class="hooks__desc">{{ row.description }}</div>
          </div>
        </div>
      </q-td>
    </template>
    <template #body-cell-events="{ row }">
      <q-td class="hooks__wrap">{{ describeEvents(row.events) }}</q-td>
    </template>
    <template #body-cell-scope="{ row }">
      <q-td class="hooks__wrap">
        <span :class="{ 'text-faint': !row.repositories.length }">{{ describeScope(row.repositories) }}</span>
      </q-td>
    </template>
    <template #body-cell-last="{ row }">
      <q-td>
        <LastDelivery :delivery="row.last_delivery" />
      </q-td>
    </template>
    <template #body-cell-enabled="{ row }">
      <q-td @click.stop>
        <q-toggle :model-value="row.enabled" dense :aria-label="t('webhooks.action.enableWhat', { name: row.name })" @update:model-value="(on: boolean) => emit('toggle', row, on)" />
      </q-td>
    </template>
    <template #body-cell-actions="{ row }">
      <q-td class="text-right hooks__actions" @click.stop>
        <WebhookActions :webhook="row" :testing="testing === row.id" @test="emit('test', row)" @deliveries="emit('deliveries', row)" @edit="emit('edit', row)" @delete="emit('delete', row)" />
      </q-td>
    </template>

    <template #item="{ row }">
      <div class="hooks__card" :class="{ 'hooks--off': !row.enabled }">
        <div class="hooks__title">
          <div class="hooks__icon"><q-icon :name="iconWebhook" size="18px" /></div>
          <div class="hooks__ident col">
            <div class="hooks__name">{{ row.name }}</div>
            <div class="hooks__url mono">{{ row.url }}</div>
          </div>
          <q-toggle :model-value="row.enabled" dense :aria-label="t('webhooks.action.enableWhat', { name: row.name })" @update:model-value="(on: boolean) => emit('toggle', row, on)" />
        </div>
        <div class="hooks__desc">{{ describeEvents(row.events) }} · {{ describeScope(row.repositories) }}</div>
        <LastDelivery :delivery="row.last_delivery" />
        <WebhookActions :webhook="row" :testing="testing === row.id" @test="emit('test', row)" @deliveries="emit('deliveries', row)" @edit="emit('edit', row)" @delete="emit('delete', row)" />
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

import type { Webhook } from '@/api/types'
import { t } from '@/i18n'
import { iconWebhook } from '@/utils/icons'

import LastDelivery from './LastDelivery.vue'
import WebhookActions from './WebhookActions.vue'
import { describeEvents, describeScope } from './webhooks'

defineProps<{ webhooks: Webhook[]; loading: boolean; testing: string | null }>()
const emit = defineEmits<{ edit: [hook: Webhook]; test: [hook: Webhook]; deliveries: [hook: Webhook]; delete: [hook: Webhook]; toggle: [hook: Webhook, enabled: boolean] }>()
const $q = useQuasar()

const columns = computed<QTableColumn<Webhook>[]>(() => [
  { name: 'name', label: t('webhooks.col.name'), field: 'name', align: 'left' },
  { name: 'events', label: t('webhooks.col.events'), field: (r) => r.events.join(','), align: 'left' },
  { name: 'scope', label: t('webhooks.col.scope'), field: (r) => r.repositories.join(','), align: 'left' },
  { name: 'last', label: t('webhooks.col.last'), field: (r) => r.last_delivery?.created_at ?? '', align: 'left' },
  { name: 'enabled', label: t('webhooks.col.enabled'), field: 'enabled', align: 'left' },
  { name: 'actions', label: '', field: 'id', align: 'right' },
])
</script>

<style scoped>
.hooks__title {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.hooks__icon {
  display: grid;
  place-items: center;
  flex: none;
  width: 30px;
  height: 30px;
  border-radius: var(--radius-sm);
  background: var(--surface-brand);
  color: var(--q-primary);
}

.hooks__ident {
  min-width: 0;
}

.hooks__name {
  font-weight: 600;
}

.hooks__url {
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.hooks__desc {
  max-width: 300px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  white-space: normal;
}

.hooks--off .hooks__name {
  color: var(--text-secondary);
}

.hooks__wrap {
  max-width: 220px;
  white-space: normal;
  font-size: var(--fs-13);
}

.hooks__actions {
  white-space: nowrap;
  padding-left: 0 !important;
  padding-right: 8px !important;
}

.hooks__card {
  width: 100%;
  display: grid;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
</style>
