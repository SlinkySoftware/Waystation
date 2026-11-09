<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- API tokens of any owner: name and prefix, owner (optional), scopes and repository limits, created, expiry, last use
     (time and address), status, revoke. Cards on phones. -->
<template>
  <q-table
    class="ws-table"
    flat
    :rows="tokens"
    :columns="columns"
    row-key="id"
    :loading="loading"
    :grid="$q.screen.lt.sm"
    :pagination="{ rowsPerPage: 0 }"
    hide-pagination
  >
    <template #body-cell-token="{ row }">
      <q-td :class="{ 'token-table__inactive': row.status !== 'active' }">
        <div class="token-table__name">{{ row.name }}</div>
        <div class="token-table__sub mono">{{ row.prefix }}…</div>
      </q-td>
    </template>
    <template #body-cell-owner="{ row }">
      <q-td><TokenOwner :owner="row.owner" /></q-td>
    </template>
    <template #body-cell-scopes="{ row }">
      <q-td><TokenScopes :token="row" /></q-td>
    </template>
    <template #body-cell-created="{ row }">
      <q-td><TimeCell :value="row.created_at" /></q-td>
    </template>
    <template #body-cell-expires="{ row }">
      <q-td><TimeCell :value="row.expires_at" :placeholder="t('tokens.never')" /></q-td>
    </template>
    <template #body-cell-lastUsed="{ row }">
      <q-td>
        <TimeCell :value="row.last_used_at" :placeholder="t('tokens.neverUsed')" />
        <div v-if="row.last_used_ip" class="token-table__sub mono">{{ row.last_used_ip }}</div>
      </q-td>
    </template>
    <template #body-cell-status="{ row }">
      <q-td><TokenStatus :status="row.status" /></q-td>
    </template>
    <template #body-cell-actions="{ row }">
      <q-td class="text-right">
        <q-btn v-if="row.status === 'active'" flat round dense size="sm" color="negative" :icon="iconBlocked" :aria-label="t('tokens.revokeWhat', { name: row.name })" @click="confirmRevoke(row)">
          <q-tooltip>{{ t('tokens.revokeWhat', { name: row.name }) }}</q-tooltip>
        </q-btn>
      </q-td>
    </template>

    <template #item="{ row }">
      <div class="token-table__card" :class="{ 'token-table__inactive': row.status !== 'active' }">
        <div class="token-table__card-head">
          <div class="col">
            <div class="token-table__name">{{ row.name }}</div>
            <div class="token-table__sub mono">{{ row.prefix }}…</div>
          </div>
          <TokenStatus :status="row.status" />
        </div>
        <TokenOwner v-if="showOwner" :owner="row.owner" />
        <TokenScopes :token="row" />
        <div class="token-table__card-meta">
          <span>{{ t('tokens.col.expires') }}: <TimeCell :value="row.expires_at" :placeholder="t('tokens.never')" /></span>
          <span>{{ t('tokens.col.lastUsed') }}: <TimeCell :value="row.last_used_at" :placeholder="t('tokens.neverUsed')" /></span>
        </div>
        <q-btn v-if="row.status === 'active'" outline no-caps color="negative" :icon="iconBlocked" :label="t('tokens.revoke')" @click="confirmRevoke(row)" />
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

import type { ApiToken } from '@/api/types'
import TimeCell from '@/components/TimeCell.vue'
import TokenScopes from '@/components/tokens/TokenScopes.vue'
import TokenStatus from '@/components/tokens/TokenStatus.vue'
import { t } from '@/i18n'
import { iconBlocked } from '@/utils/icons'

import TokenOwner from './TokenOwner.vue'
import { useProblemDialog } from './useAdminActions'

const props = withDefaults(defineProps<{ tokens: ApiToken[]; showOwner?: boolean; loading?: boolean; revoke: (token: ApiToken) => Promise<void> }>(), { showOwner: false, loading: false })
const emit = defineEmits<{ revoked: [token: ApiToken] }>()
const $q = useQuasar()
const showProblem = useProblemDialog()

const columns = computed<QTableColumn<ApiToken>[]>(() => [
  { name: 'token', label: t('admin.tokensPage.col.token'), field: 'name', align: 'left' },
  ...(props.showOwner ? [{ name: 'owner', label: t('admin.tokensPage.col.owner'), field: (r: ApiToken) => r.owner.name, align: 'left' as const }] : []),
  { name: 'scopes', label: t('admin.tokensPage.col.scopes'), field: (r) => r.scopes.join(','), align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'created', label: t('admin.tokensPage.col.created'), field: 'created_at', align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
  { name: 'expires', label: t('admin.tokensPage.col.expires'), field: (r) => r.expires_at ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'lastUsed', label: t('admin.tokensPage.col.lastUsed'), field: (r) => r.last_used_at ?? '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'status', label: t('admin.tokensPage.col.status'), field: 'status', align: 'left' },
  { name: 'actions', label: '', field: 'id', align: 'right' },
])

function confirmRevoke(token: ApiToken): void {
  $q.dialog({
    title: t('admin.tokensPage.revokeTitle', { name: token.name, owner: token.owner.name }),
    message: t('admin.tokensPage.revokeText'),
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'negative', label: t('tokens.revoke') },
    persistent: true,
  }).onOk(() => {
    props
      .revoke(token)
      .then(() => emit('revoked', token))
      .catch(showProblem)
  })
}
</script>

<style scoped>
.token-table__name {
  font-weight: 600;
}

.token-table__sub {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.token-table__inactive {
  opacity: 0.65;
}

.token-table__card {
  width: 100%;
  display: grid;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.token-table__card-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.token-table__card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}
</style>
