<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Set a local user's password (one-time by default). Every session of the user ends. -->
<template>
  <FormDialog v-model="open" :title="t('admin.reset.title', { name: user?.username ?? '' })" :caption="t('admin.reset.caption')" :saving="saving" :submit-label="t('admin.reset.submit')" @submit="save">
    <ProblemBanner :error="problem" />
    <div class="reset-form">
      <PasswordField v-model="password" :username="user?.username ?? ''" :server-error="fieldError" />
      <q-checkbox v-model="mustChange" dense :label="t('admin.userForm.mustChange')" />
      <div class="reset-form__hint">{{ t('admin.reset.copyHint') }}</div>
    </div>
  </FormDialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

import { api, ApiError, call } from '@/api/client'
import type { UserDetail, UserSummary } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'

import { generatePassword } from './forms'
import PasswordField from './PasswordField.vue'
import ProblemBanner from './ProblemBanner.vue'

const props = defineProps<{ user: UserSummary | UserDetail | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [] }>()
const notify = useNotify()

const password = ref('')
const mustChange = ref(true)
const saving = ref(false)
const problem = ref<unknown>(null)
const fieldError = ref<string | null>(null)

watch(open, (isOpen) => {
  if (!isOpen) return
  password.value = generatePassword()
  mustChange.value = true
  problem.value = null
  fieldError.value = null
})

async function save(): Promise<void> {
  if (!props.user) return
  saving.value = true
  problem.value = null
  fieldError.value = null
  try {
    await call(api.POST('/users/{user_id}/password', { params: { path: { user_id: props.user.id } }, body: { password: password.value, must_change_password: mustChange.value } }))
    notify.success(t('admin.reset.done', { name: props.user.username }))
    open.value = false
    emit('saved')
  } catch (error) {
    if (error instanceof ApiError) fieldError.value = error.fieldErrors.password ?? null
    problem.value = error
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.reset-form {
  display: grid;
  gap: 12px;
}

.reset-form__hint {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
