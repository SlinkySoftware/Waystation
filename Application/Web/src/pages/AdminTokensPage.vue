<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Administration → API tokens (UI_DESIGN §4.10, SPEC §4.9): every token of every user and service account (find,
     see last use, revoke) and the service accounts themselves (create, roles, tokens shown once, permissions). -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.apiTokens')" :caption="t('admin.tokensPage.caption')" />

    <q-card class="admin-tokens">
      <q-tabs v-model="tab" dense no-caps align="left" active-color="primary" indicator-color="primary" class="admin-tokens__tabs">
        <q-tab name="tokens" :icon="iconToken" :label="t('admin.tokensPage.tabTokens')" />
        <q-tab name="accounts" :icon="iconServiceAccount" :label="t('admin.tokensPage.tabAccounts')" />
      </q-tabs>

      <template v-if="tab === 'tokens'">
        <div class="admin-tokens__toolbar">
          <q-input v-model="search" dense outlined clearable debounce="150" :placeholder="t('admin.tokensPage.search')" class="admin-tokens__search">
            <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
          </q-input>
          <q-select v-model="owner" dense outlined emit-value map-options options-dense :options="ownerOptions" class="admin-tokens__filter" :aria-label="t('admin.tokensPage.col.owner')" />
          <q-select v-model="status" dense outlined emit-value map-options options-dense :options="statusOptions" class="admin-tokens__filter" :aria-label="t('admin.tokensPage.col.status')" />
          <q-space />
          <span class="text-faint admin-tokens__count">{{ countText }}</span>
        </div>
        <div v-if="list.error.value && !list.data.value" class="admin-tokens__error" role="alert">
          {{ t('admin.tokensPage.loadFailed') }}: {{ errorMessage(list.error.value) }}
          <q-btn flat dense no-caps :label="t('common.retry')" @click="list.reload" />
        </div>
        <TokenTable v-else :tokens="rows" show-owner :loading="list.loading.value && !list.data.value" :revoke="revoke" @revoked="list.reload">
          <template #empty>
            <EmptyState v-if="!list.loading.value" class="full-width" :icon="iconToken" :title="all.length ? t('admin.tokensPage.noMatch') : t('admin.tokensPage.emptyTitle')" :text="all.length ? undefined : t('admin.tokensPage.emptyText')" />
          </template>
        </TokenTable>
      </template>

      <ServiceAccountsTable v-else @changed="list.reload" />
    </q-card>
  </q-page>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { ApiToken } from '@/api/types'
import ServiceAccountsTable from '@/components/admin/ServiceAccountsTable.vue'
import TokenTable from '@/components/admin/TokenTable.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { iconSearch, iconServiceAccount, iconToken } from '@/utils/icons'

const LIMIT = 500 // the server lists the newest 500
const notify = useNotify()
const tabParam = useQueryParam('tab')
const tab = computed({ get: () => (tabParam.value === 'accounts' ? 'accounts' : 'tokens'), set: (v: string) => (tabParam.value = v === 'tokens' ? null : v) })
const search = useQueryParam('q')
const ownerParam = useQueryParam('owner')
const statusParam = useQueryParam('status')
const owner = computed({ get: () => ownerParam.value ?? 'all', set: (v: string) => (ownerParam.value = v === 'all' ? null : v) })
const status = computed({ get: () => statusParam.value ?? 'active', set: (v: string) => (statusParam.value = v === 'active' ? null : v) })

const list = useAsync(() => call(api.GET('/tokens')))
const all = computed(() => list.data.value ?? [])
const rows = computed(() => {
  const needle = (search.value ?? '').trim().toLowerCase()
  return all.value.filter(
    (tok) =>
      (owner.value === 'all' || tok.owner.kind === owner.value) &&
      (status.value === 'all' || tok.status === status.value) &&
      (!needle || tok.name.toLowerCase().includes(needle) || tok.prefix.toLowerCase().includes(needle) || tok.owner.name.toLowerCase().includes(needle)),
  )
})
const countText = computed(() => (all.value.length >= LIMIT ? t('admin.tokensPage.capped', { count: LIMIT }) : t('admin.tokensPage.shown', { count: rows.value.length })))

const ownerOptions = computed(() => [
  { value: 'all', label: t('admin.tokensPage.ownerAll') },
  { value: 'user', label: t('admin.tokensPage.ownerUsers') },
  { value: 'service_account', label: t('admin.tokensPage.ownerAccounts') },
])
const statusOptions = computed(() => [
  { value: 'all', label: t('admin.tokensPage.statusAll') },
  { value: 'active', label: t('tokens.status.active') },
  { value: 'expired', label: t('tokens.status.expired') },
  { value: 'revoked', label: t('tokens.status.revoked') },
])

async function revoke(token: ApiToken): Promise<void> {
  await call(api.DELETE('/tokens/{token_id}', { params: { path: { token_id: token.id } } }))
  notify.success(t('admin.tokensPage.revoked', { name: token.name }))
}
</script>

<style scoped>
.admin-tokens {
  overflow: hidden;
}

.admin-tokens__tabs {
  border-bottom: 1px solid var(--border);
  padding: 0 8px;
}

.admin-tokens__toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.admin-tokens__search {
  flex: 1 1 240px;
  max-width: 360px;
}

.admin-tokens__filter {
  flex: 0 1 180px;
  min-width: 140px;
}

.admin-tokens__count {
  font-size: var(--fs-13);
}

.admin-tokens__error {
  padding: 16px;
  color: var(--cache-error);
}
</style>
