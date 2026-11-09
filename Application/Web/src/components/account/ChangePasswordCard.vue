<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Change your own local password (SPEC §4.9). Shown on the profile page for local accounts; while a one-time
     password is pending it is the only thing the server allows, so the router keeps the user here. -->
<template>
  <PageCard :title="t('profile.password.title')" :caption="t('profile.password.caption')">
    <div v-if="oneTime" class="password-card__notice" role="alert">
      <q-icon :name="iconWarning" size="18px" />
      {{ t('profile.password.oneTime') }}
    </div>
    <q-form class="password-card__form" autocomplete="off" @submit="save">
      <div v-if="formError" class="form-error" role="alert">{{ formError }}</div>
      <q-input
        v-model="current"
        outlined
        dense
        type="password"
        autocomplete="current-password"
        :label="t('profile.password.current')"
        :rules="[(v: string) => Boolean(v) || t('profile.password.required')]"
        :error="Boolean(fieldErrors.current_password)"
        :error-message="fieldErrors.current_password"
      />
      <q-input
        v-model="next"
        outlined
        dense
        type="password"
        autocomplete="new-password"
        :label="t('profile.password.new')"
        :rules="[(v: string) => (v ?? '').length >= 12 || t('profile.password.tooShort')]"
        :error="Boolean(fieldErrors.new_password)"
        :error-message="fieldErrors.new_password"
      />
      <q-input
        v-model="confirm"
        outlined
        dense
        type="password"
        autocomplete="new-password"
        :label="t('profile.password.confirm')"
        :rules="[(v: string) => v === next || t('profile.password.mismatch')]"
      />
      <div>
        <q-btn type="submit" unelevated color="primary" no-caps :loading="saving" :label="t('profile.password.submit')" />
      </div>
    </q-form>
  </PageCard>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { ApiError, errorMessage } from '@/api/client'
import PageCard from '@/components/PageCard.vue'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { iconWarning } from '@/utils/icons'

const auth = useAuthStore()
const notify = useNotify()
const oneTime = computed(() => auth.mustChangePassword)
const current = ref('')
const next = ref('')
const confirm = ref('')
const saving = ref(false)
const formError = ref<string | null>(null)
const fieldErrors = ref<Record<string, string>>({})

async function save(): Promise<void> {
  saving.value = true
  formError.value = null
  fieldErrors.value = {}
  try {
    await auth.changePassword(current.value, next.value)
    current.value = next.value = confirm.value = ''
    notify.success(t('profile.password.changed'))
  } catch (error) {
    if (error instanceof ApiError && error.status === 422 && Object.keys(error.fieldErrors).length) {
      fieldErrors.value = error.fieldErrors
    } else {
      formError.value = errorMessage(error)
    }
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.password-card__form {
  display: grid;
  gap: 8px;
  max-width: 420px;
}

.password-card__notice {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  font-weight: 500;
}
</style>
