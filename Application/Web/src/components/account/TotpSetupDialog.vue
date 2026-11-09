<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Setting up two-factor sign-in (SPEC §4.9): the current password, then the QR code (drawn in the browser) and the
     setup key, then a code from the app. Emits the recovery codes the server returns once; the secret is dropped when
     the dialog closes. -->
<template>
  <FormDialog
    v-model="open"
    :title="t('profile.totp.title')"
    :caption="setup ? t('profile.totp.scan') : t('profile.totp.passwordHint')"
    :saving="saving"
    :submit-label="setup ? t('profile.totp.confirm') : t('profile.totp.start')"
    width="520px"
    @submit="submit"
  >
    <ProblemBanner :error="problem" />
    <template v-if="!setup">
      <q-input
        v-model="password"
        outlined
        dense
        autofocus
        type="password"
        autocomplete="current-password"
        :label="t('profile.totp.password')"
        :rules="[(v: string) => Boolean(v) || t('profile.totp.codeRequired')]"
        :error="Boolean(fieldErrors.password)"
        :error-message="fieldErrors.password"
      />
    </template>
    <template v-else>
      <div class="setup">
        <QrCode :value="setup.otpauth_uri" :label="t('profile.totp.qrLabel', { account: setup.account })" :size="184" class="setup__qr" />
        <div class="setup__side">
          <div class="setup__label">{{ t('profile.totp.key') }}</div>
          <CopyField :value="grouped" :label="t('profile.totp.key')" />
          <div class="setup__meta text-faint">{{ setup.issuer }} · {{ setup.account }}</div>
          <q-input
            v-model="code"
            outlined
            dense
            autofocus
            inputmode="numeric"
            autocomplete="one-time-code"
            maxlength="6"
            input-class="mono"
            :label="t('profile.totp.code')"
            :hint="t('profile.totp.codeHint')"
            :rules="[(v: string) => /^\d{6}$/.test(v.trim()) || t('profile.totp.codeHint')]"
            :error="Boolean(fieldErrors.code)"
            :error-message="fieldErrors.code"
          />
        </div>
      </div>
    </template>
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call } from '@/api/client'
import type { TotpSetup } from '@/api/types'
import ProblemBanner from '@/components/admin/ProblemBanner.vue'
import CopyField from '@/components/CopyField.vue'
import FormDialog from '@/components/FormDialog.vue'
import { t } from '@/i18n'

import QrCode from './QrCode.vue'

const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ enabled: [codes: string[]] }>()

const password = ref('')
const code = ref('')
const setup = ref<TotpSetup | null>(null)
const saving = ref(false)
const problem = ref<unknown>(null)
const fieldErrors = ref<Record<string, string>>({})
/** The key in groups of four, easier to type into an app. */
const grouped = computed(() => (setup.value?.secret.match(/.{1,4}/g) ?? []).join(' '))

watch(open, (value) => {
  if (value) return
  password.value = code.value = ''
  setup.value = null
  problem.value = null
  fieldErrors.value = {}
})

async function submit(): Promise<void> {
  saving.value = true
  problem.value = null
  fieldErrors.value = {}
  try {
    if (!setup.value) {
      setup.value = await call(api.POST('/auth/me/totp/setup', { body: { password: password.value } }))
      password.value = ''
      return
    }
    const result = await call(api.POST('/auth/me/totp/confirm', { body: { code: code.value.trim() } }))
    open.value = false
    emit('enabled', result.recovery_codes)
  } catch (error) {
    if (error instanceof ApiError && error.status === 422 && Object.keys(error.fieldErrors).length) fieldErrors.value = error.fieldErrors
    else problem.value = error
    code.value = ''
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.setup {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
  align-items: flex-start;
}

.setup__qr {
  flex: none;
  border: 1px solid var(--border);
}

.setup__side {
  display: grid;
  flex: 1;
  gap: 8px;
  min-width: 220px;
}

.setup__label {
  font-size: var(--fs-13);
  font-weight: 600;
}

.setup__meta {
  font-size: var(--fs-12);
}
</style>
