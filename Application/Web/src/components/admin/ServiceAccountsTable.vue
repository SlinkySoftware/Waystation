<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Service accounts: name and description, roles, active tokens, created, status; open (tokens, permissions), edit,
     disable/enable, delete. -->
<template>
  <div>
    <div class="accounts__toolbar">
      <span class="text-muted">{{ t('admin.accounts.caption') }}</span>
      <q-btn unelevated no-caps color="primary" :icon="iconAdd" :label="t('admin.accounts.create')" @click="edit(null)" />
    </div>
    <q-table
      class="ws-table"
      flat
      :rows="list.data.value ?? []"
      :columns="columns"
      row-key="id"
      :loading="list.loading.value && !list.data.value"
      :grid="$q.screen.lt.sm"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      @row-click="(_e: Event, row: ServiceAccount) => openPanel(row, 'tokens')"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <div class="accounts__name">
            <q-icon :name="iconServiceAccount" size="20px" class="accounts__icon" />
            <div class="accounts__text">
              <div class="accounts__title mono">{{ row.name }}</div>
              <div class="accounts__desc">{{ row.description }}</div>
            </div>
          </div>
        </q-td>
      </template>
      <template #body-cell-roles="{ row }">
        <q-td class="cursor-pointer"><RoleChips :roles="row.roles" /></q-td>
      </template>
      <template #body-cell-tokens="{ row }">
        <q-td class="cursor-pointer num">{{ row.active_tokens }}</q-td>
      </template>
      <template #body-cell-created="{ row }">
        <q-td class="cursor-pointer"><TimeCell :value="row.created_at" /></q-td>
      </template>
      <template #body-cell-status="{ row }">
        <q-td class="cursor-pointer">
          <span class="ws-badge" :class="row.disabled ? 'tone-blocked' : 'tone-ok'">
            <q-icon :name="row.disabled ? iconBlocked : iconOk" />
            {{ row.disabled ? t('admin.status.disabled') : t('admin.status.active') }}
          </span>
        </q-td>
      </template>
      <template #body-cell-actions="{ row }">
        <q-td class="text-right" @click.stop>
          <q-btn flat round dense size="sm" :icon="iconMore" :aria-label="row.name">
            <q-menu auto-close anchor="bottom right" self="top right">
              <q-list dense style="min-width: 200px">
                <q-item clickable @click="openPanel(row, 'tokens')">
                  <q-item-section side><q-icon :name="iconToken" size="18px" /></q-item-section>
                  <q-item-section>{{ t('admin.accounts.tokens') }}</q-item-section>
                </q-item>
                <q-item clickable @click="openPanel(row, 'permissions')">
                  <q-item-section side><q-icon :name="iconShield" size="18px" /></q-item-section>
                  <q-item-section>{{ t('admin.accounts.permissions') }}</q-item-section>
                </q-item>
                <q-item clickable @click="edit(row)">
                  <q-item-section side><q-icon :name="iconEdit" size="18px" /></q-item-section>
                  <q-item-section>{{ t('admin.accounts.edit') }}</q-item-section>
                </q-item>
                <q-item clickable @click="toggle(row)">
                  <q-item-section side><q-icon :name="row.disabled ? iconPersonOn : iconPersonOff" size="18px" /></q-item-section>
                  <q-item-section>{{ row.disabled ? t('admin.accounts.enable') : t('admin.accounts.disable') }}</q-item-section>
                </q-item>
                <q-separator />
                <q-item clickable class="text-negative" @click="confirmDelete(row)">
                  <q-item-section side><q-icon :name="iconDelete" size="18px" color="negative" /></q-item-section>
                  <q-item-section>{{ t('admin.accounts.delete') }}</q-item-section>
                </q-item>
              </q-list>
            </q-menu>
          </q-btn>
        </q-td>
      </template>

      <template #item="{ row }">
        <div class="accounts__card" role="button" tabindex="0" @click="openPanel(row, 'tokens')" @keydown.enter="openPanel(row, 'tokens')">
          <div class="accounts__name">
            <q-icon :name="iconServiceAccount" size="20px" class="accounts__icon" />
            <div class="accounts__text col">
              <div class="accounts__title mono">{{ row.name }}</div>
              <div class="accounts__desc">{{ row.description }}</div>
            </div>
            <span class="ws-badge" :class="row.disabled ? 'tone-blocked' : 'tone-ok'">{{ row.disabled ? t('admin.status.disabled') : t('admin.status.active') }}</span>
          </div>
          <RoleChips :roles="row.roles" />
          <div class="text-muted accounts__meta">{{ t('admin.accounts.col.tokens') }}: {{ row.active_tokens }}</div>
        </div>
      </template>

      <template #no-data>
        <EmptyState v-if="list.error.value" class="full-width" illustration="error" :title="t('admin.accounts.loadFailed')" :text="errorMessage(list.error.value)">
          <q-btn flat no-caps :label="t('common.retry')" @click="list.reload" />
        </EmptyState>
        <EmptyState v-else-if="!list.loading.value" class="full-width" :icon="iconServiceAccount" :title="t('admin.accounts.emptyTitle')" :text="t('admin.accounts.emptyText')">
          <q-btn unelevated no-caps color="primary" :label="t('admin.accounts.create')" @click="edit(null)" />
        </EmptyState>
      </template>
    </q-table>

    <ServiceAccountDialog v-model="editing" :account="current" @saved="onSaved" />
    <ServiceAccountPanel v-model="panelOpen" :account="current" :initial-tab="panelTab" @changed="changed" />
  </div>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { ServiceAccount } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { iconAdd, iconBlocked, iconDelete, iconEdit, iconMore, iconOk, iconPersonOff, iconPersonOn, iconServiceAccount, iconShield, iconToken } from '@/utils/icons'

import RoleChips from './RoleChips.vue'
import ServiceAccountDialog from './ServiceAccountDialog.vue'
import ServiceAccountPanel from './ServiceAccountPanel.vue'
import { useProblemDialog } from './useAdminActions'

const emit = defineEmits<{ changed: [] }>()
const $q = useQuasar()
const notify = useNotify()
const showProblem = useProblemDialog()
const list = useAsync(() => call(api.GET('/service-accounts')))

const columns = computed<QTableColumn<ServiceAccount>[]>(() => [
  { name: 'name', label: t('admin.accounts.col.name'), field: 'name', align: 'left', sortable: true },
  { name: 'roles', label: t('admin.accounts.col.roles'), field: (r) => r.roles.join(','), align: 'left' },
  { name: 'tokens', label: t('admin.accounts.col.tokens'), field: 'active_tokens', align: 'right', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'created', label: t('admin.accounts.col.created'), field: 'created_at', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'status', label: t('admin.accounts.col.status'), field: 'disabled', align: 'left' },
  { name: 'actions', label: '', field: 'id', align: 'right' },
])

const editing = ref(false)
const panelOpen = ref(false)
const panelTab = ref<'tokens' | 'permissions'>('tokens')
const current = ref<ServiceAccount | null>(null)

function edit(account: ServiceAccount | null): void {
  current.value = account
  editing.value = true
}

function openPanel(account: ServiceAccount, tab: 'tokens' | 'permissions'): void {
  current.value = account
  panelTab.value = tab
  panelOpen.value = true
}

function changed(): void {
  void list.reload()
  emit('changed')
}

function onSaved(account: ServiceAccount): void {
  notify.success(t(current.value ? 'admin.accounts.saved' : 'admin.accounts.created', { name: account.name }))
  changed()
}

async function toggle(account: ServiceAccount): Promise<void> {
  try {
    await call(api.PATCH('/service-accounts/{account_id}', { params: { path: { account_id: account.id } }, body: { disabled: !account.disabled } }))
    notify.success(t(account.disabled ? 'admin.accounts.enabled' : 'admin.accounts.disabled', { name: account.name }))
    changed()
  } catch (error) {
    showProblem(error)
  }
}

function confirmDelete(account: ServiceAccount): void {
  $q.dialog({
    title: t('admin.accounts.deleteTitle', { name: account.name }),
    message: t('admin.accounts.deleteText'),
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'negative', label: t('common.delete') },
    persistent: true,
  }).onOk(async () => {
    try {
      await call(api.DELETE('/service-accounts/{account_id}', { params: { path: { account_id: account.id } } }))
      notify.success(t('admin.accounts.deleted', { name: account.name }))
      changed()
    } catch (error) {
      showProblem(error)
    }
  })
}
</script>

<style scoped>
.accounts__toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  font-size: var(--fs-13);
}

.accounts__name {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.accounts__icon {
  color: var(--q-primary);
  flex: none;
}

.accounts__text {
  min-width: 0;
}

.accounts__title {
  font-weight: 600;
}

.accounts__desc {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 380px;
}

.accounts__card {
  width: calc(100% - 24px);
  margin: 0 12px 8px;
  padding: 12px;
  display: grid;
  gap: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  cursor: pointer;
}

.accounts__meta {
  font-size: var(--fs-12);
}
</style>
