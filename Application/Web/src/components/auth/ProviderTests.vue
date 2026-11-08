<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- "Test connection" / "Test discovery" (OIDC) and "Test user lookup" (LDAP) against the current, possibly unsaved,
     form (UI_DESIGN §4.10). -->
<template>
  <div class="ptests">
    <div class="ptests__block">
      <div class="ptests__bar">
        <q-btn outline dense no-caps :icon="iconSync" :label="isOidc ? t('oidcTest.discovery') : t('ldapTest.connection')" :loading="connBusy" :disable="!hasServer" @click="runConnection" />
        <span class="text-caption text-muted">{{ isOidc ? t('oidcTest.discoveryHint') : t('ldapTest.connectionHint') }}</span>
      </div>
      <div v-if="conn" class="ptests__result" :class="conn.ok ? 'ptests__result--ok' : 'ptests__result--bad'" role="status">
        <div class="ptests__summary">
          <q-icon :name="conn.ok ? iconOk : iconError" size="18px" />
          <strong v-if="isOidc">{{ conn.ok ? t('oidcTest.allOk') : t('oidcTest.failed') }}</strong>
          <strong v-else>{{ conn.ok ? t('ldapTest.allOk') : t('ldapTest.someFailed', { failed: conn.servers.filter((s) => !s.ok).length, total: conn.servers.length }) }}</strong>
        </div>
        <div v-for="s in conn.servers" :key="s.server" class="ptests__server">
          <div class="ptests__server-head">
            <q-icon :name="s.ok ? iconCheck : iconClose" size="16px" :class="s.ok ? 'text-positive' : 'text-negative'" />
            <span class="mono">{{ s.server }}</span>
            <span v-if="s.latency_ms !== null" class="text-muted num">· {{ t('ldapTest.latency', { ms: Math.round(s.latency_ms) }) }}</span>
            <span v-if="s.tls" class="text-muted">· {{ s.tls }}</span>
          </div>
          <CheckList :checks="s.checks" />
        </div>
        <dl v-if="conn.discovery" class="ptests__facts">
          <dt>{{ t('oidcTest.authorization') }}</dt>
          <dd class="mono">{{ conn.discovery.authorization_endpoint }}</dd>
          <dt>{{ t('oidcTest.token') }}</dt>
          <dd class="mono">{{ conn.discovery.token_endpoint }}</dd>
          <dt>{{ t('oidcTest.userinfo') }}</dt>
          <dd :class="{ mono: conn.discovery.userinfo_endpoint, 'text-muted': !conn.discovery.userinfo_endpoint }">{{ conn.discovery.userinfo_endpoint ?? t('oidcTest.notOffered') }}</dd>
          <dt>{{ t('oidcTest.endSession') }}</dt>
          <dd :class="{ mono: conn.discovery.end_session_endpoint, 'text-muted': !conn.discovery.end_session_endpoint }">{{ conn.discovery.end_session_endpoint ?? t('oidcTest.notOffered') }}</dd>
          <dt>{{ t('oidcTest.jwks') }}</dt>
          <dd><span class="mono">{{ conn.discovery.jwks_uri }}</span> · {{ t('oidcTest.keys', { count: conn.discovery.keys }) }}</dd>
          <template v-if="conn.discovery.signing_algs">
            <dt>{{ t('oidcTest.algs') }}</dt>
            <dd class="mono">{{ conn.discovery.signing_algs.join(', ') }}</dd>
          </template>
        </dl>
      </div>
      <div v-else-if="connError" class="ptests__result ptests__result--bad" role="alert">{{ connError }}</div>
    </div>

    <div v-if="!isOidc" class="ptests__block">
      <div class="ptests__user">
        <q-input v-model="username" outlined dense class="col" :label="t('ldapTest.username')" autocomplete="off" spellcheck="false" @keydown.enter.prevent="runUser" />
        <q-input v-model="password" outlined dense class="col" type="password" autocomplete="new-password" :label="t('ldapTest.password')" @keydown.enter.prevent="runUser" />
        <q-btn outline no-caps :icon="iconPerson" :label="t('ldapTest.lookup')" :loading="userBusy" :disable="!username.trim() || !hasServer" class="ptests__lookup" @click="runUser" />
      </div>
      <div class="text-caption text-muted">{{ t('ldapTest.userHint') }}</div>
      <div v-if="user" class="ptests__result" :class="user.ok ? 'ptests__result--ok' : 'ptests__result--bad'" role="status">
        <div class="ptests__summary">
          <q-icon :name="user.ok ? iconOk : iconError" size="18px" />
          <strong>{{ user.found && user.user ? t('ldapTest.found', { name: user.user.display_name }) : t('ldapTest.notFound') }}</strong>
          <span v-if="user.server" class="text-muted">· {{ t('ldapTest.via', { server: user.server }) }}</span>
        </div>
        <dl v-if="user.user" class="ptests__facts">
          <dt>{{ t('ldapTest.dn') }}</dt>
          <dd class="mono">{{ user.user.dn }}</dd>
          <dt>{{ t('ldapTest.email') }}</dt>
          <dd>{{ user.user.email ?? '—' }}</dd>
          <dt>{{ t('ldapTest.groups') }}</dt>
          <dd>
            <span v-if="!user.groups.length" class="text-muted">{{ t('ldapTest.noGroups') }}</span>
            <q-chip v-for="g in user.groups" :key="g.id" dense square class="ptests__chip" :label="g.name">
              <q-tooltip>{{ g.id }}</q-tooltip>
            </q-chip>
          </dd>
          <template v-if="user.require_group_ok !== null">
            <dt>{{ t('ldapTest.requireGroup') }}</dt>
            <dd :class="user.require_group_ok ? 'text-positive' : 'text-negative'">{{ user.require_group_ok ? t('ldapTest.requireOk') : t('ldapTest.requireFailed') }}</dd>
          </template>
          <dt>{{ t('ldapTest.passwordResult') }}</dt>
          <dd :class="{ 'text-positive': user.password === 'ok', 'text-negative': user.password === 'failed' }">{{ t(`ldapTest.password_${user.password}`) }}</dd>
          <dt>{{ t('ldapTest.roles') }}</dt>
          <dd>
            <span v-if="!user.roles.length" class="text-muted">{{ t('ldapTest.noRoles') }}</span>
            <q-chip v-for="r in user.roles" :key="r" dense square color="primary" text-color="white" class="ptests__chip" :label="r" />
          </dd>
        </dl>
        <CheckList :checks="user.checks" />
      </div>
      <div v-else-if="userError" class="ptests__result ptests__result--bad" role="alert">{{ userError }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { AuthProviderConnectionTest, AuthProviderUserTest } from '@/api/types'
import { t } from '@/i18n'
import { iconCheck, iconClose, iconError, iconOk, iconPerson, iconSync } from '@/utils/icons'

import CheckList from './CheckList.vue'
import { type ProviderFormModel, toWrite } from './providerForm'

const props = defineProps<{ model: ProviderFormModel; providerId: string | null }>()

const isOidc = computed(() => props.model.write.kind === 'oidc')
const hasServer = computed(() => (isOidc.value ? !!props.model.write.oidc.issuer.trim() : props.model.write.ldap.connection.servers.some((s) => s.trim())))
const connBusy = ref(false)
const conn = ref<AuthProviderConnectionTest | null>(null)
const connError = ref<string | null>(null)
const username = ref('')
const password = ref('')
const userBusy = ref(false)
const user = ref<AuthProviderUserTest | null>(null)
const userError = ref<string | null>(null)

watch(isOidc, () => {
  conn.value = null
  connError.value = null
})

function describe(cause: unknown): string {
  return cause instanceof ApiError && cause.problem.errors?.length ? cause.problem.errors.map((e) => `${e.field}: ${e.message}`).join(' ') : errorMessage(cause)
}

const base = () => ({ provider: toWrite(props.model), ...(props.providerId ? { id: props.providerId } : {}) })

async function runConnection(): Promise<void> {
  connBusy.value = true
  conn.value = null
  connError.value = null
  try {
    conn.value = await call(api.POST('/auth-providers/test-connection', { body: base() }))
  } catch (cause) {
    connError.value = describe(cause)
  } finally {
    connBusy.value = false
  }
}

async function runUser(): Promise<void> {
  if (!username.value.trim() || userBusy.value) return
  userBusy.value = true
  user.value = null
  userError.value = null
  try {
    user.value = await call(api.POST('/auth-providers/test-user', { body: { ...base(), username: username.value.trim(), password: password.value || null } }))
  } catch (cause) {
    userError.value = describe(cause)
  } finally {
    userBusy.value = false
  }
}
</script>

<style scoped>
.ptests {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ptests__block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ptests__bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 12px;
}

.ptests__user {
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 8px;
}

.ptests__user > .col {
  min-width: 160px;
}

.ptests__lookup {
  height: 40px;
}

.ptests__result {
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-13);
}

.ptests__result--ok {
  background: var(--status-ok-bg);
}

.ptests__result--bad {
  background: var(--cache-error-bg);
}

.ptests__summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.ptests__result--ok .ptests__summary {
  color: var(--status-ok);
}

.ptests__result--bad .ptests__summary,
.ptests__result--bad[role='alert'] {
  color: var(--cache-error);
}

.ptests__server {
  margin-top: 8px;
  color: var(--text-primary);
}

.ptests__server-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: var(--fs-13);
}

.ptests__facts {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 4px 14px;
  margin: 8px 0 4px;
  color: var(--text-primary);
}

.ptests__facts dt {
  color: var(--text-secondary);
  font-weight: 550;
}

.ptests__facts dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.ptests__chip {
  margin: 0 4px 2px 0;
}
</style>
