<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Two-factor sign-in on the profile page (SPEC §4.9, UI_DESIGN §4.11): state, set up (QR code drawn locally),
     new recovery codes, turn off. When policy requires it, this is the only thing the server allows until it is on,
     so the router keeps the user here. -->
<template>
  <PageCard :title="t('profile.totp.title')" :caption="t('profile.totp.caption')">
    <template v-if="state.data.value?.enabled" #actions>
      <q-btn flat no-caps :label="t('profile.totp.newCodes')" @click="openCodes" />
      <q-btn outline no-caps color="negative" :label="t('profile.totp.turnOff')" @click="openOff" />
    </template>
    <div v-if="state.data.value?.required" class="totp__notice" role="alert">
      <q-icon :name="iconWarning" size="18px" />
      {{ t('profile.totp.required') }}
    </div>
    <ProblemBanner v-if="state.error.value" :error="state.error.value" />
    <div v-else-if="state.data.value && !state.data.value.available" class="text-faint">
      {{ t('profile.totp.unavailable', { source: auth.user?.source ?? '' }) }}
    </div>
    <div v-else-if="state.data.value" class="totp__state">
      <span class="ws-badge" :class="state.data.value.enabled ? 'tone-ok' : 'tone-offline'" data-testid="totp-state">
        <q-icon :name="state.data.value.enabled ? iconOk : iconTwoFactor" />
        {{ state.data.value.enabled ? t('profile.totp.on', { time: absoluteTime(state.data.value.enabled_at ?? '') }) : t('profile.totp.off') }}
      </span>
      <span v-if="state.data.value.enabled" :class="lowCodes ? 'totp__low' : 'text-faint'">
        {{ lowCodes ? t('profile.totp.recoveryLow', { count: state.data.value.recovery_codes_left }) : t('profile.totp.recoveryLeft', { count: state.data.value.recovery_codes_left }) }}
      </span>
      <q-btn v-else unelevated no-caps color="primary" :icon="iconTwoFactor" :label="t('profile.totp.setUp')" @click="settingUp = true" />
    </div>
    <div class="totp__clients text-faint">{{ t('profile.totp.clients') }}</div>

    <TotpSetupDialog v-model="settingUp" @enabled="onEnabled" />
    <RecoveryCodesDialog v-model="showingCodes" :codes="codes" :account="auth.user?.username ?? ''" @closed="codes = []" />

    <FormDialog v-model="turningOff" :title="t('profile.totp.offTitle')" :caption="t('profile.totp.offText')" :saving="saving" :submit-label="t('profile.totp.turnOff')" width="460px" @submit="turnOff">
      <ProblemBanner :error="problem" />
      <q-input v-model="password" outlined dense type="password" autocomplete="current-password" :label="t('profile.totp.password')" :rules="[required]" :error="Boolean(fieldErrors.password)" :error-message="fieldErrors.password" />
      <q-input v-model="code" outlined dense autocomplete="one-time-code" input-class="mono" :label="t('profile.totp.offCode')" :rules="[required]" :error="Boolean(fieldErrors.code)" :error-message="fieldErrors.code" />
    </FormDialog>

    <FormDialog v-model="renewing" :title="t('profile.totp.newCodesTitle')" :caption="t('profile.totp.newCodesText')" :saving="saving" :submit-label="t('profile.totp.make')" width="460px" @submit="renew">
      <ProblemBanner :error="problem" />
      <q-input v-model="code" outlined dense inputmode="numeric" autocomplete="one-time-code" maxlength="6" input-class="mono" :label="t('profile.totp.code')" :rules="[required]" :error="Boolean(fieldErrors.code)" :error-message="fieldErrors.code" />
    </FormDialog>
  </PageCard>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { api, ApiError, call } from '@/api/client'
import ProblemBanner from '@/components/admin/ProblemBanner.vue'
import FormDialog from '@/components/FormDialog.vue'
import PageCard from '@/components/PageCard.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { absoluteTime } from '@/utils/format'
import { iconOk, iconTwoFactor, iconWarning } from '@/utils/icons'

import RecoveryCodesDialog from './RecoveryCodesDialog.vue'
import TotpSetupDialog from './TotpSetupDialog.vue'

const auth = useAuthStore()
const notify = useNotify()
const state = useAsync(() => call(api.GET('/auth/me/totp')))
const lowCodes = computed(() => (state.data.value?.recovery_codes_left ?? 10) <= 3)

const settingUp = ref(false)
const showingCodes = ref(false)
const codes = ref<string[]>([])
const turningOff = ref(false)
const renewing = ref(false)
const password = ref('')
const code = ref('')
const saving = ref(false)
const problem = ref<unknown>(null)
const fieldErrors = ref<Record<string, string>>({})
const required = (v: string): boolean | string => Boolean(v.trim()) || t('profile.totp.codeRequired')

function reset(): void {
  password.value = code.value = ''
  problem.value = null
  fieldErrors.value = {}
}

function openOff(): void {
  reset()
  turningOff.value = true
}

function openCodes(): void {
  reset()
  renewing.value = true
}

async function onEnabled(recovery: string[]): Promise<void> {
  codes.value = recovery
  showingCodes.value = true
  notify.success(t('profile.totp.enabled'))
  await Promise.all([state.reload(), auth.refresh()])
}

async function run(action: () => Promise<void>): Promise<void> {
  saving.value = true
  problem.value = null
  fieldErrors.value = {}
  try {
    await action()
  } catch (error) {
    if (error instanceof ApiError && error.status === 422 && Object.keys(error.fieldErrors).length) fieldErrors.value = error.fieldErrors
    else problem.value = error
    code.value = ''
  } finally {
    saving.value = false
  }
}

async function turnOff(): Promise<void> {
  await run(async () => {
    await call(api.POST('/auth/me/totp/disable', { body: { password: password.value, code: code.value.trim() } }))
    turningOff.value = false
    notify.success(t('profile.totp.turnedOff'))
    await Promise.all([state.reload(), auth.refresh()])
  })
}

async function renew(): Promise<void> {
  await run(async () => {
    const result = await call(api.POST('/auth/me/totp/recovery-codes', { body: { code: code.value.trim() } }))
    renewing.value = false
    codes.value = result.recovery_codes
    showingCodes.value = true
    await state.reload()
  })
}
</script>

<style scoped>
.totp__notice {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  color: var(--status-degraded);
  background: var(--status-degraded-bg);
  font-weight: 500;
}

.totp__state {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}

.totp__low {
  color: var(--status-degraded);
  font-weight: 500;
}

.totp__clients {
  margin-top: 12px;
  font-size: var(--fs-12);
}
</style>
