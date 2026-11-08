<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Administration → Auth providers (UI_DESIGN §4.10): LDAP directories people sign in with, their group mapping. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.authProviders')" :caption="caption">
      <template #actions>
        <q-btn unelevated no-caps color="primary" :icon="iconAdd" :label="t('authProviders.create')" @click="edit(null)" />
      </template>
    </PageHeader>

    <q-card class="providers">
      <div v-if="list.error.value && !list.data.value" class="providers__error" role="alert">
        {{ t('authProviders.loadFailed') }} {{ errorMessage(list.error.value) }}
        <q-btn flat dense no-caps :label="t('common.retry')" @click="list.reload" />
      </div>
      <q-table
        v-else
        class="ws-table"
        flat
        :rows="rows"
        :columns="columns"
        row-key="id"
        :loading="list.loading.value && !list.data.value"
        :grid="$q.screen.lt.sm"
        :pagination="{ rowsPerPage: 0 }"
        hide-pagination
        @row-click="(_e: Event, row: AuthProviderConfig) => edit(row)"
      >
        <template #body-cell-name="{ row }">
          <q-td class="cursor-pointer">
            <div class="providers__name">
              <q-icon :name="iconAuthProviders" size="20px" class="providers__icon" />
              <span class="providers__title">{{ row.name }}</span>
            </div>
          </q-td>
        </template>
        <template #body-cell-kind="{ row }">
          <q-td><span class="providers__kind">{{ row.kind.toUpperCase() }}</span></q-td>
        </template>
        <template #body-cell-state="{ row }">
          <q-td><StateLabel :enabled="row.enabled" /></q-td>
        </template>
        <template #body-cell-servers="{ row }">
          <q-td>
            <span class="mono providers__server">{{ serverHost(row.ldap.connection.servers[0] ?? '') }}</span>
            <span v-if="row.ldap.connection.servers.length > 1" class="text-muted providers__more">
              {{ t('authProviders.moreServers', { count: row.ldap.connection.servers.length - 1 }) }}
              <q-tooltip>{{ row.ldap.connection.servers.map(serverHost).join(', ') }}</q-tooltip>
            </span>
          </q-td>
        </template>
        <template #body-cell-security="{ row }">
          <q-td>
            <span class="providers__security" :class="{ 'providers__security--weak': row.ldap.connection.security === 'none' }">
              <q-icon :name="row.ldap.connection.security === 'none' ? iconWarning : iconLock" size="15px" />
              {{ securityLabel(row) }}
            </span>
          </q-td>
        </template>
        <template #body-cell-actions="{ row }">
          <q-td class="text-right" @click.stop>
            <q-btn flat round dense size="sm" :icon="iconEdit" :aria-label="`${t('common.edit')} ${row.name}`" @click="edit(row)" />
            <q-btn flat round dense size="sm" :icon="iconDelete" :aria-label="`${t('common.delete')} ${row.name}`" @click="confirmDelete(row)" />
          </q-td>
        </template>

        <template #item="{ row }">
          <div class="providers__card" role="button" tabindex="0" @click="edit(row)" @keydown.enter="edit(row)">
            <div class="providers__name">
              <q-icon :name="iconAuthProviders" size="20px" class="providers__icon" />
              <span class="providers__title">{{ row.name }}</span>
              <q-space />
              <StateLabel :enabled="row.enabled" />
            </div>
            <div class="providers__card-meta">
              <span class="mono">{{ serverHost(row.ldap.connection.servers[0] ?? '') }}</span>
              <span>{{ securityLabel(row) }}</span>
              <span>{{ t('authProviders.mappingCount', { count: row.group_mapping.length }) }}</span>
            </div>
          </div>
        </template>

        <template #no-data>
          <EmptyState class="full-width" :icon="iconAuthProviders" :title="t('authProviders.emptyTitle')" :text="t('authProviders.emptyText')">
            <q-btn unelevated no-caps color="primary" :label="t('authProviders.create')" @click="edit(null)" />
          </EmptyState>
        </template>
      </q-table>
    </q-card>

    <ProviderDialog v-model="editing" :provider="current" @saved="list.reload" />
  </q-page>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { AuthProviderConfig } from '@/api/types'
import ProviderDialog from '@/components/auth/ProviderDialog.vue'
import { serverHost } from '@/components/auth/providerForm'
import StateLabel from '@/components/auth/StateLabel.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { iconAdd, iconAuthProviders, iconDelete, iconEdit, iconLock, iconWarning } from '@/utils/icons'

const $q = useQuasar()
const notify = useNotify()
const list = useAsync<AuthProviderConfig[]>(() => call(api.GET('/auth-providers')))
const rows = computed(() => list.data.value ?? [])
const caption = computed(() => (list.data.value?.length ? t('authProviders.captionCount', { count: list.data.value.length }) : t('authProviders.caption')))

const editing = ref(false)
const current = ref<AuthProviderConfig | null>(null)

const columns = computed<QTableColumn<AuthProviderConfig>[]>(() => [
  { name: 'name', label: t('authProviders.col.name'), field: 'name', align: 'left', sortable: true },
  { name: 'kind', label: t('authProviders.col.kind'), field: 'kind', align: 'left' },
  { name: 'state', label: t('authProviders.col.state'), field: 'enabled', align: 'left', sortable: true },
  { name: 'servers', label: t('authProviders.col.servers'), field: (r) => r.ldap.connection.servers.join(', '), align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'security', label: t('authProviders.col.security'), field: (r) => r.ldap.connection.security, align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'order', label: t('authProviders.col.order'), field: 'order', align: 'right', sortable: true, classes: 'gt-sm num', headerClasses: 'gt-sm' },
  { name: 'mapping', label: t('authProviders.col.mapping'), field: (r) => r.group_mapping.length, format: (v: number) => t('authProviders.mappingCount', { count: v }), align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'actions', label: '', field: 'id', align: 'right' },
])

function securityLabel(row: AuthProviderConfig): string {
  return t(`authProviders.security.${row.ldap.connection.security}`)
}

function edit(row: AuthProviderConfig | null): void {
  current.value = row
  editing.value = true
}

function confirmDelete(row: AuthProviderConfig): void {
  $q.dialog({
    title: t('authProviders.deleteTitle', { name: row.name }),
    message: t('authProviders.deleteText'),
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'negative', label: t('common.delete') },
    persistent: true,
  }).onOk(async () => {
    try {
      await call(api.DELETE('/auth-providers/{id}', { params: { path: { id: row.id } } }))
      notify.success(t('authProviders.deleted', { name: row.name }))
      await list.reload()
    } catch (error) {
      notify.error(error)
    }
  })
}
</script>

<style scoped>
.providers {
  overflow: hidden;
}

.providers__error {
  padding: 16px;
  color: var(--cache-error);
}

.providers__name {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.providers__icon {
  color: var(--q-primary);
}

.providers__title {
  font-weight: 600;
}

.providers__kind {
  padding: 1px 7px;
  border: 1px solid var(--border-strong);
  border-radius: 4px;
  font-size: 11px;
  font-weight: 650;
  letter-spacing: 0.04em;
  color: var(--text-secondary);
}

.providers__server {
  font-size: var(--fs-12);
}

.providers__more {
  margin-left: 6px;
  font-size: var(--fs-12);
}

.providers__security {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.providers__security--weak {
  color: var(--status-degraded);
}

.providers__card {
  width: calc(100% - 24px);
  margin: 0 12px 8px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  cursor: pointer;
}

.providers__card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin: 8px 0 0 30px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
