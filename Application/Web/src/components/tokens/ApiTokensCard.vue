<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- The signed-in user's API tokens (UI_DESIGN §4.11): list, create (secret shown once), revoke. Cards on phones. -->
<template>
  <PageCard id="tokens" :title="t('tokens.title')" :caption="t('tokens.caption')" flush>
    <template #actions>
      <q-toggle v-if="inactiveCount" v-model="showInactive" dense :label="`${t('tokens.showInactive')} (${inactiveCount})`" class="tokens__toggle" />
      <q-btn unelevated no-caps color="primary" :icon="iconAdd" :label="t('tokens.create')" :disable="!list.data.value" @click="creating = true" />
    </template>

    <div v-if="list.error.value && !list.data.value" class="tokens__error" role="alert">
      {{ errorMessage(list.error.value) }}
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
    >
      <template #body-cell-name="{ row }">
        <q-td :class="{ 'tokens__inactive': row.status !== 'active' }">
          <div class="tokens__name">{{ row.name }}</div>
          <div class="tokens__prefix mono">{{ row.prefix }}…</div>
        </q-td>
      </template>
      <template #body-cell-scopes="{ row }">
        <q-td><TokenScopes :token="row" /></q-td>
      </template>
      <template #body-cell-expires="{ row }">
        <q-td><TimeCell :value="row.expires_at" :placeholder="t('tokens.never')" /></q-td>
      </template>
      <template #body-cell-lastUsed="{ row }">
        <q-td>
          <TimeCell :value="row.last_used_at" :placeholder="t('tokens.neverUsed')" />
          <div v-if="row.last_used_ip" class="tokens__ip mono">{{ row.last_used_ip }}</div>
        </q-td>
      </template>
      <template #body-cell-status="{ row }">
        <q-td><TokenStatus :status="row.status" /></q-td>
      </template>
      <template #body-cell-actions="{ row }">
        <q-td class="text-right">
          <q-btn v-if="row.status === 'active'" flat dense no-caps color="negative" :icon="iconBlocked" :label="t('tokens.revoke')" :aria-label="t('tokens.revokeWhat', { name: row.name })" @click="confirmRevoke(row)" />
        </q-td>
      </template>

      <template #item="{ row }">
        <div class="tokens__card" :class="{ 'tokens__inactive': row.status !== 'active' }">
          <div class="tokens__card-head">
            <div class="col">
              <div class="tokens__name">{{ row.name }}</div>
              <div class="tokens__prefix mono">{{ row.prefix }}…</div>
            </div>
            <TokenStatus :status="row.status" />
          </div>
          <TokenScopes :token="row" />
          <div class="tokens__card-meta">
            <span>{{ t('tokens.col.expires') }}: <TimeCell :value="row.expires_at" :placeholder="t('tokens.never')" /></span>
            <span>{{ t('tokens.col.lastUsed') }}: <TimeCell :value="row.last_used_at" :placeholder="t('tokens.neverUsed')" /></span>
          </div>
          <q-btn v-if="row.status === 'active'" outline no-caps color="negative" :icon="iconBlocked" :label="t('tokens.revoke')" @click="confirmRevoke(row)" />
        </div>
      </template>

      <template #no-data>
        <EmptyState class="full-width" compact :icon="iconToken" :title="allTokens.length ? t('tokens.noActive') : t('tokens.emptyTitle')" :text="allTokens.length ? undefined : t('tokens.emptyText')">
          <q-btn v-if="!allTokens.length" unelevated no-caps color="primary" :label="t('tokens.create')" @click="creating = true" />
        </EmptyState>
      </template>
    </q-table>

    <CreateTokenDialog v-model="creating" :available="list.data.value?.available_scopes ?? []" @created="onCreated" />
    <TokenSecretDialog v-model="showSecret" :created="created" @closed="created = null" />
  </PageCard>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { ApiToken, ApiTokenCreated } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { iconAdd, iconBlocked, iconToken } from '@/utils/icons'

import CreateTokenDialog from './CreateTokenDialog.vue'
import TokenScopes from './TokenScopes.vue'
import TokenSecretDialog from './TokenSecretDialog.vue'
import TokenStatus from './TokenStatus.vue'

const $q = useQuasar()
const notify = useNotify()
const list = useAsync(() => call(api.GET('/auth/me/tokens')))
const creating = ref(false)
const showInactive = ref(false)
const created = ref<ApiTokenCreated | null>(null)
const showSecret = ref(false)

const allTokens = computed(() => list.data.value?.items ?? [])
const inactiveCount = computed(() => allTokens.value.filter((tok) => tok.status !== 'active').length)
const rows = computed(() => (showInactive.value ? allTokens.value : allTokens.value.filter((tok) => tok.status === 'active')))

const columns = computed<QTableColumn<ApiToken>[]>(() => [
  { name: 'name', label: t('tokens.col.name'), field: 'name', align: 'left' },
  { name: 'scopes', label: t('tokens.col.scopes'), field: (r) => r.scopes.join(','), align: 'left' },
  { name: 'expires', label: t('tokens.col.expires'), field: (r) => r.expires_at ?? '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'lastUsed', label: t('tokens.col.lastUsed'), field: (r) => r.last_used_at ?? '', align: 'left' },
  { name: 'status', label: t('tokens.col.status'), field: 'status', align: 'left' },
  { name: 'actions', label: '', field: 'id', align: 'right' },
])

function onCreated(value: ApiTokenCreated): void {
  created.value = value
  showSecret.value = true
  void list.reload()
}

function confirmRevoke(token: ApiToken): void {
  $q.dialog({
    title: t('tokens.revokeTitle', { name: token.name }),
    message: t('tokens.revokeText'),
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'negative', label: t('tokens.revoke') },
    persistent: true,
  }).onOk(async () => {
    try {
      await call(api.DELETE('/auth/me/tokens/{token_id}', { params: { path: { token_id: token.id } } }))
      notify.success(t('tokens.revoked', { name: token.name }))
      await list.reload()
    } catch (error) {
      notify.error(error)
    }
  })
}
</script>

<style scoped>
.tokens__toggle {
  font-size: var(--fs-13);
}

.tokens__error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px;
  color: var(--cache-error);
}

.tokens__name {
  font-weight: 600;
}

.tokens__prefix,
.tokens__ip {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.tokens__inactive {
  opacity: 0.65;
}

.tokens__card {
  width: 100%;
  display: grid;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.tokens__card-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.tokens__card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}
</style>
