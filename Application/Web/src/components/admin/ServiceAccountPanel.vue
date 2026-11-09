<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- One service account in a side panel: its tokens (create, shown once; revoke) and what it may do. -->
<template>
  <q-dialog v-model="open" position="right" :maximized="$q.screen.lt.md" full-height>
    <q-card class="sa-panel">
      <header class="sa-panel__head">
        <q-icon :name="iconServiceAccount" size="24px" class="sa-panel__icon" />
        <div class="col">
          <div class="sa-panel__title">{{ account?.name }}</div>
          <div class="sa-panel__caption">{{ account?.description }}</div>
        </div>
        <q-btn v-close-popup flat round dense :icon="iconClose" :aria-label="t('common.close')" />
      </header>
      <q-tabs v-model="tab" dense no-caps align="left" active-color="primary" indicator-color="primary" class="sa-panel__tabs">
        <q-tab name="tokens" :label="t('admin.accounts.tokens')" />
        <q-tab name="permissions" :label="t('admin.accounts.permissions')" />
      </q-tabs>
      <div class="sa-panel__body">
        <template v-if="tab === 'tokens'">
          <div class="sa-panel__actions">
            <span class="text-muted">{{ t('admin.accounts.tokensCaption') }}</span>
            <q-btn unelevated no-caps color="primary" :icon="iconAdd" :label="t('admin.accounts.createToken')" :disable="!tokens.data.value || account?.disabled" @click="creating = true" />
          </div>
          <div v-if="tokens.error.value" class="text-negative">{{ errorMessage(tokens.error.value) }}</div>
          <TokenTable :tokens="tokens.data.value?.items ?? []" :loading="tokens.loading.value && !tokens.data.value" :revoke="revoke" @revoked="changed">
            <template #empty><div class="sa-panel__empty text-faint">{{ t('tokens.emptyTitle') }}</div></template>
          </TokenTable>
        </template>
        <template v-else>
          <PermissionsView v-if="permissions.data.value" :data="permissions.data.value" />
          <div v-else-if="permissions.error.value" class="text-negative">{{ errorMessage(permissions.error.value) }}</div>
          <q-skeleton v-else height="160px" />
        </template>
      </div>
    </q-card>
    <CreateTokenDialog v-model="creating" :available="tokens.data.value?.available_scopes ?? []" :account-id="account?.id ?? null" @created="onCreated" />
    <TokenSecretDialog v-model="showSecret" :created="created" @closed="created = null" />
  </q-dialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { ApiToken, ApiTokenCreated, ServiceAccount } from '@/api/types'
import CreateTokenDialog from '@/components/tokens/CreateTokenDialog.vue'
import TokenSecretDialog from '@/components/tokens/TokenSecretDialog.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { iconAdd, iconClose, iconServiceAccount } from '@/utils/icons'

import PermissionsView from './PermissionsView.vue'
import TokenTable from './TokenTable.vue'

const props = defineProps<{ account: ServiceAccount | null; initialTab?: 'tokens' | 'permissions' }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ changed: [] }>()
const notify = useNotify()

const tab = ref<'tokens' | 'permissions'>('tokens')
const creating = ref(false)
const created = ref<ApiTokenCreated | null>(null)
const showSecret = ref(false)

const accountId = () => (open.value ? props.account?.id : undefined)
const tokens = useAsync(async () => {
  const id = accountId()
  return id ? call(api.GET('/service-accounts/{account_id}/tokens', { params: { path: { account_id: id } } })) : null
}, [accountId])
const permissions = useAsync(async () => {
  const id = accountId()
  return id ? call(api.GET('/service-accounts/{account_id}/permissions', { params: { path: { account_id: id } } })) : null
}, [accountId])

watch(open, (isOpen) => {
  if (isOpen) tab.value = props.initialTab ?? 'tokens'
})

function changed(): void {
  void tokens.reload()
  emit('changed')
}

function onCreated(value: ApiTokenCreated): void {
  created.value = value
  showSecret.value = true
  changed()
}

async function revoke(token: ApiToken): Promise<void> {
  if (!props.account) return
  await call(api.DELETE('/service-accounts/{account_id}/tokens/{token_id}', { params: { path: { account_id: props.account.id, token_id: token.id } } }))
  notify.success(t('admin.tokensPage.revoked', { name: token.name }))
}
</script>

<style scoped>
.sa-panel {
  width: 760px;
  max-width: 100vw;
  display: flex;
  flex-direction: column;
}

.sa-panel__head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 18px 20px 12px;
}

.sa-panel__icon {
  color: var(--q-primary);
}

.sa-panel__title {
  font-size: var(--fs-16);
  font-weight: 650;
}

.sa-panel__caption {
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.sa-panel__tabs {
  border-bottom: 1px solid var(--border);
  padding: 0 12px;
}

.sa-panel__body {
  padding: 16px 20px;
  overflow-y: auto;
  flex: 1;
}

.sa-panel__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;
  font-size: var(--fs-13);
}

.sa-panel__empty {
  padding: 16px;
}
</style>
