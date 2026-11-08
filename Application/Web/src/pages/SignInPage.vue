<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Sign-in (UI_DESIGN §4.1): centred card, local/LDAP form, one button per OIDC provider, generic errors. -->
<template>
  <q-page class="sign-in">
    <div class="sign-in__panel">
      <div class="sign-in__brand">
        <BrandMark :size="44" />
        <div>
          <div class="sign-in__product">{{ config.config?.instance_name ?? 'Waystation' }}</div>
          <div class="sign-in__tagline">{{ t('signIn.tagline') }}</div>
        </div>
      </div>

      <q-card class="sign-in__card">
        <h1 class="sign-in__title">{{ t('signIn.title') }}</h1>
        <div v-if="expired" class="sign-in__note" role="status">{{ t('signIn.expired') }}</div>
        <q-form class="sign-in__form" @submit="submit">
          <template v-if="passwordProviders.length > 1">
            <label class="sign-in__label" for="provider">{{ t('signIn.provider') }}</label>
            <q-select
              id="provider"
              v-model="provider"
              outlined
              dense
              emit-value
              map-options
              options-dense
              :options="providerOptions"
              :disable="busy"
              :hint="provider ? undefined : t('signIn.providerAutoHint')"
            />
          </template>
          <label class="sign-in__label" for="username">{{ t('signIn.username') }}</label>
          <q-input
            id="username"
            v-model="username"
            outlined
            dense
            autofocus
            autocomplete="username"
            :disable="busy"
            :rules="[(v: string) => !!v || t('signIn.usernameRequired')]"
            hide-bottom-space
          />
          <label class="sign-in__label" for="password">{{ t('signIn.password') }}</label>
          <q-input
            id="password"
            v-model="password"
            outlined
            dense
            :type="showPassword ? 'text' : 'password'"
            autocomplete="current-password"
            :disable="busy"
            :rules="[(v: string) => !!v || t('signIn.passwordRequired')]"
            hide-bottom-space
          >
            <template #append>
              <q-btn flat round dense size="sm" :icon="showPassword ? iconHide : iconShow" :aria-label="showPassword ? t('signIn.hidePassword') : t('signIn.showPassword')" @click="showPassword = !showPassword" />
            </template>
          </q-input>
          <div v-if="error" class="sign-in__error" role="alert">
            <q-icon :name="iconError" size="18px" />
            {{ error }}
          </div>
          <q-btn type="submit" unelevated no-caps color="primary" class="sign-in__submit" :label="t('signIn.submit')" :loading="busy" />
        </q-form>

        <template v-if="oidc.length">
          <div class="sign-in__or"><span>{{ t('signIn.or') }}</span></div>
          <q-btn v-for="p in oidc" :key="p.id" outline no-caps class="sign-in__oidc" :label="t('signIn.continueWith', { name: p.name })" :href="p.login_url" />
        </template>
      </q-card>
      <div v-if="mock" class="sign-in__mock">{{ t('signIn.mockHint') }}</div>
      <div class="sign-in__foot">{{ t('signIn.footer', { version: config.config?.version ?? '' }) }}</div>
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, ApiError, call } from '@/api/client'
import type { AuthProvider } from '@/api/types'
import BrandMark from '@/components/BrandMark.vue'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { safeNext } from '@/router/guard'
import { useAuthStore } from '@/stores/auth'
import { useConfigStore } from '@/stores/config'
import { iconError, iconHide, iconShow } from '@/utils/icons'

const auth = useAuthStore()
const config = useConfigStore()
const route = useRoute()
const router = useRouter()
const mock = import.meta.env.WAYSTATION_MOCK === '1'

const username = ref('')
const password = ref('')
const showPassword = ref(false)
const busy = ref(false)
const error = ref<string | null>(null)
const expired = computed(() => route.query.expired === '1' && !error.value)

const providers = useAsync<AuthProvider[]>(() => call(api.GET('/auth/providers')))
const oidc = computed(() => (providers.data.value ?? []).filter((p) => p.kind === 'oidc' && p.login_url))
// Local and LDAP share the username/password form (UI_DESIGN §4.1): a select appears only when there are several.
const passwordProviders = computed(() => (providers.data.value ?? []).filter((p) => p.kind === 'local' || p.kind === 'ldap'))
const provider = ref<string | null>(null)
const providerOptions = computed(() => [{ label: t('signIn.providerAuto'), value: null }, ...passwordProviders.value.map((p) => ({ label: p.name, value: p.id }))])

async function submit(): Promise<void> {
  busy.value = true
  error.value = null
  try {
    await auth.login(username.value.trim(), password.value, provider.value ?? undefined)
    await router.replace(safeNext(route.query.next))
  } catch (cause) {
    password.value = ''
    // Generic message: never say which part was wrong (UI_DESIGN §4.1).
    error.value = cause instanceof ApiError && cause.status === 429 ? t('signIn.tooMany') : cause instanceof ApiError && cause.status === 401 ? t('signIn.failed') : t('signIn.unavailable')
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.sign-in {
  display: grid;
  place-items: center;
  min-height: 100vh;
  padding: 24px 16px;
  background:
    radial-gradient(1200px 600px at 15% -10%, var(--brand-100), transparent 60%),
    radial-gradient(900px 500px at 110% 110%, var(--surface-brand), transparent 60%),
    var(--page-bg);
}

.sign-in__panel {
  width: 100%;
  max-width: 400px;
}

.sign-in__brand {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 24px;
  justify-content: center;
}

.sign-in__product {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.02em;
}

.sign-in__tagline {
  color: var(--text-secondary);
  font-size: var(--fs-13);
}

.sign-in__card {
  padding: 28px 28px 24px;
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-pop);
}

.sign-in__title {
  margin: 0 0 18px;
  line-height: 1.3;
  font-size: var(--fs-20);
  font-weight: 650;
  letter-spacing: -0.01em;
}

.sign-in__form {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.sign-in__label {
  margin-top: 6px;
  font-size: var(--fs-13);
  font-weight: 550;
  color: var(--text-secondary);
}

.sign-in__error,
.sign-in__note {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-13);
}

.sign-in__error {
  color: var(--cache-error);
  background: var(--cache-error-bg);
}

.sign-in__note {
  margin: 0 0 12px;
  color: var(--status-degraded);
  background: var(--status-degraded-bg);
}

.sign-in__submit {
  margin-top: 16px;
  height: 40px;
}

.sign-in__or {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 18px 0 12px;
  color: var(--text-tertiary);
  font-size: var(--fs-12);
}

.sign-in__or::before,
.sign-in__or::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}

.sign-in__oidc {
  width: 100%;
  height: 40px;
  margin-bottom: 8px;
}

.sign-in__mock {
  margin-top: 16px;
  padding: 8px 12px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-sm);
  font-size: var(--fs-12);
  color: var(--text-secondary);
  text-align: center;
}

.sign-in__foot {
  margin-top: 20px;
  text-align: center;
  font-size: var(--fs-12);
  color: var(--text-tertiary);
}
</style>
