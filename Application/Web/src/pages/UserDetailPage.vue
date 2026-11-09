<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Administration → Users → one user: account facts and linked identities, roles and groups (local, mapped and what
     the provider last reported), effective permissions, sessions and API tokens. -->
<template>
  <q-page class="ws-page">
    <PageHeader :crumbs="[{ label: t('admin.detail.back'), to: { name: 'admin-users' } }, { label: user?.username ?? '…' }]">
      <template #icon><UserCell v-if="user" :user="user" class="detail__avatar-only" /></template>
      <template #title>{{ user?.display_name ?? user?.username ?? '…' }}</template>
      <template #caption>
        <span v-if="user" class="detail__caption">
          <span class="mono">{{ user.username }}</span>
          <SourceBadge :source="user.source" />
          <UserStatus :user="user" />
          <span v-if="self" class="text-faint">{{ t('admin.users.self') }}</span>
        </span>
      </template>
      <template v-if="user" #actions>
        <q-btn outline no-caps :icon="iconEdit" :label="t('admin.users.edit')" @click="editing = true" />
        <q-btn v-if="user.source === 'local'" outline no-caps :icon="iconPassword" :label="t('admin.users.resetPassword')" @click="resetting = true" />
        <q-btn v-if="user.locked" outline no-caps :icon="iconUnlock" :label="t('admin.users.unlock')" @click="actions.unlock(user)" />
        <UserRowMenu :user="user" @edit="editing = true" @reset="resetting = true" @changed="onChanged" @deleted="router.push({ name: 'admin-users' })" />
      </template>
    </PageHeader>

    <q-card v-if="detail.error.value && !user">
      <EmptyState illustration="notFound" :title="t('admin.detail.loadFailed')" :text="errorMessage(detail.error.value)">
        <q-btn flat no-caps :label="t('admin.detail.back')" :to="{ name: 'admin-users' }" />
      </EmptyState>
    </q-card>

    <div v-else-if="user" class="detail">
      <div class="detail__col">
        <PageCard :title="t('admin.detail.account')">
          <dl class="detail__facts">
            <div><dt>{{ t('admin.detail.username') }}</dt><dd class="mono">{{ user.username }}</dd></div>
            <div><dt>{{ t('admin.detail.email') }}</dt><dd>{{ user.email ?? '–' }}</dd></div>
            <div><dt>{{ t('admin.detail.source') }}</dt><dd>{{ sourceText }}</dd></div>
            <div><dt>{{ t('admin.detail.created') }}</dt><dd><TimeCell :value="user.created_at" /></dd></div>
            <div><dt>{{ t('admin.detail.lastLogin') }}</dt><dd><TimeCell :value="user.last_login" :placeholder="t('admin.users.neverSignedIn')" /></dd></div>
            <div v-if="user.source === 'local'"><dt>{{ t('admin.detail.password') }}</dt><dd>{{ user.has_password ? t('admin.detail.passwordSet') : t('admin.detail.passwordNone') }}</dd></div>
          </dl>
        </PageCard>

        <PageCard :title="t('admin.detail.access')">
          <div class="detail__block">
            <div class="detail__label">{{ t('admin.detail.directRoles') }}</div>
            <RoleChips :roles="user.roles" :max="8" />
          </div>
          <div class="detail__block">
            <div class="detail__label">{{ t('admin.detail.groups') }}</div>
            <div v-if="!user.group_refs.length" class="text-faint">{{ t('admin.detail.groupsEmpty') }}</div>
            <ul v-else class="detail__list">
              <li v-for="g in user.group_refs" :key="g.id">
                <router-link :to="{ name: 'admin-groups', query: { open: g.id } }" class="text-link">{{ g.name }}</router-link>
                <SourceBadge :source="g.source" />
              </li>
            </ul>
          </div>
          <div v-if="user.source !== 'local' || user.external_groups.length" class="detail__block">
            <div class="detail__label">{{ t('admin.detail.reported') }}</div>
            <div v-if="!user.external_groups.length" class="text-faint">{{ t('admin.detail.reportedEmpty') }}</div>
            <ul v-else class="detail__list">
              <li v-for="g in user.external_groups" :key="`${g.provider} ${g.id}`">
                <span>{{ g.name }}</span>
                <span v-if="g.id !== g.name" class="mono text-faint detail__dn">{{ g.id }}</span>
                <SourceBadge :source="g.provider" />
              </li>
            </ul>
          </div>
        </PageCard>

        <PageCard :title="t('admin.detail.identities')">
          <div v-if="!user.identities.length" class="text-faint">{{ t('admin.detail.identitiesEmpty') }}</div>
          <ul v-else class="detail__list detail__list--stacked">
            <li v-for="i in user.identities" :key="`${i.provider} ${i.subject}`">
              <div class="detail__identity">
                <SourceBadge :source="i.provider" />
                <span class="detail__strong">{{ directory.providerName(i.provider) }}</span>
              </div>
              <div class="detail__identity-meta">
                <span>{{ t('admin.detail.subject') }}: <span class="mono">{{ i.subject }}</span></span>
                <span>{{ t('admin.detail.linked') }} <TimeCell :value="i.linked_at" /></span>
                <span>{{ t('admin.detail.lastLogin') }} <TimeCell :value="i.last_login" :placeholder="t('admin.users.neverSignedIn')" /></span>
              </div>
            </li>
          </ul>
        </PageCard>
      </div>

      <div class="detail__col">
        <PageCard :title="t('admin.permissions.title')" :caption="t('admin.permissions.caption')">
          <PermissionsView v-if="permissions.data.value" :data="permissions.data.value" />
          <div v-else-if="permissions.error.value" class="text-negative">{{ t('admin.permissions.loadFailed') }}: {{ errorMessage(permissions.error.value) }}</div>
          <q-skeleton v-else height="160px" />
        </PageCard>

        <PageCard :title="t('admin.detail.sessions')" :caption="t('admin.detail.sessionsCaption')">
          <template #actions>
            <q-btn v-if="sessions.data.value?.length" flat no-caps :icon="iconLogout" :label="t('admin.users.signOut')" @click="actions.signOut(user)" />
          </template>
          <SessionsTable v-if="sessions.data.value" :sessions="sessions.data.value" />
          <div v-else-if="sessions.error.value" class="text-negative">{{ errorMessage(sessions.error.value) }}</div>
          <q-skeleton v-else height="60px" />
        </PageCard>

        <PageCard :title="t('admin.detail.tokens')" :caption="t('admin.detail.tokensCaption')" flush>
          <TokenTable :tokens="userTokens" :loading="tokens.loading.value && !tokens.data.value" :revoke="revoke" @revoked="tokens.reload">
            <template #empty><div class="detail__empty text-faint">{{ t('tokens.emptyTitle') }}</div></template>
          </TokenTable>
        </PageCard>
      </div>
    </div>

    <UserDialog v-model="editing" :user="user" @saved="onSaved" />
    <ResetPasswordDialog v-model="resetting" :user="user" @saved="refreshAll" />
  </q-page>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, call, errorMessage } from '@/api/client'
import type { ApiToken, UserDetail } from '@/api/types'
import PermissionsView from '@/components/admin/PermissionsView.vue'
import ResetPasswordDialog from '@/components/admin/ResetPasswordDialog.vue'
import RoleChips from '@/components/admin/RoleChips.vue'
import SessionsTable from '@/components/admin/SessionsTable.vue'
import SourceBadge from '@/components/admin/SourceBadge.vue'
import TokenTable from '@/components/admin/TokenTable.vue'
import { useUserActions } from '@/components/admin/useAdminActions'
import UserCell from '@/components/admin/UserCell.vue'
import UserDialog from '@/components/admin/UserDialog.vue'
import UserRowMenu from '@/components/admin/UserRowMenu.vue'
import UserStatus from '@/components/admin/UserStatus.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useDirectoryStore } from '@/stores/directory'
import { iconEdit, iconLogout, iconPassword, iconUnlock } from '@/utils/icons'

const route = useRoute()
const router = useRouter()
const notify = useNotify()
const auth = useAuthStore()
const directory = useDirectoryStore()
onMounted(() => {
  void directory.loadRoles().catch(() => undefined)
  void directory.loadProviders()
})

const id = computed(() => String(route.params.id))
const path = () => ({ params: { path: { user_id: id.value } } })
const detail = useAsync(() => call(api.GET('/users/{user_id}', path())), [id])
const permissions = useAsync(() => call(api.GET('/users/{user_id}/permissions', path())), [id])
const sessions = useAsync(() => call(api.GET('/users/{user_id}/sessions', path())), [id])
const tokens = useAsync(() => call(api.GET('/tokens')), [id])

const user = computed(() => detail.data.value)
const self = computed(() => auth.user?.id === user.value?.id)
const userTokens = computed(() => (tokens.data.value ?? []).filter((tok) => tok.owner.kind === 'user' && tok.owner.id === id.value))
const sourceText = computed(() => {
  const u = user.value
  if (!u) return ''
  return u.source === 'local' ? t('admin.source.localHelp') : t('admin.source.externalHelp', { provider: directory.providerName(u.source) ?? u.source })
})

const editing = ref(false)
const resetting = ref(false)

function refreshAll(): void {
  void detail.reload()
  void permissions.reload()
  void sessions.reload()
}

function onChanged(updated: UserDetail | null): void {
  if (updated) detail.data.value = updated
  refreshAll()
  void tokens.reload()
}

const actions = useUserActions(onChanged)

function onSaved(updated: UserDetail): void {
  notify.success(t('admin.users.saved', { name: updated.username }))
  onChanged(updated)
}

async function revoke(token: ApiToken): Promise<void> {
  await call(api.DELETE('/tokens/{token_id}', { params: { path: { token_id: token.id } } }))
  notify.success(t('admin.tokensPage.revoked', { name: token.name }))
}
</script>

<style scoped>
.detail {
  display: grid;
  grid-template-columns: minmax(0, 5fr) minmax(0, 7fr);
  gap: 16px;
  align-items: start;
}

@media (max-width: 1100px) {
  .detail {
    grid-template-columns: minmax(0, 1fr);
  }
}

.detail__col {
  display: grid;
  gap: 16px;
  min-width: 0;
}

.detail__caption {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.detail__avatar-only :deep(.user-cell__text) {
  display: none;
}

.detail__facts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px 24px;
  margin: 0;
}

.detail__facts dt,
.detail__label {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.detail__facts dd {
  margin: 2px 0 0;
  font-weight: 500;
  overflow-wrap: anywhere;
}

.detail__block + .detail__block {
  margin-top: 14px;
}

.detail__label {
  margin-bottom: 6px;
}

.detail__list {
  display: grid;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.detail__list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  min-width: 0;
}

.detail__list--stacked li {
  display: grid;
  gap: 4px;
}

.detail__dn {
  font-size: var(--fs-12);
  overflow-wrap: anywhere;
}

.detail__identity {
  display: flex;
  align-items: center;
  gap: 8px;
}

.detail__identity-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 14px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.detail__strong {
  font-weight: 600;
}

.detail__empty {
  padding: 16px;
}
</style>
