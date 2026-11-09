<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Create a service account (name, description, roles) or edit one (description, roles, disabled). -->
<template>
  <FormDialog
    v-model="open"
    :title="account ? t('admin.accountForm.editTitle', { name: account.name }) : t('admin.accountForm.createTitle')"
    :caption="t('admin.accounts.caption')"
    :saving="saving"
    :submit-label="account ? t('common.save') : t('admin.accounts.create')"
    @submit="save"
  >
    <ProblemBanner :error="problem" />
    <div class="account-form">
      <q-input
        v-model="model.name"
        outlined
        dense
        maxlength="63"
        input-class="mono"
        :autofocus="!account"
        :readonly="Boolean(account)"
        :label="t('admin.accountForm.name')"
        :hint="account ? undefined : t('admin.accountForm.nameHint')"
        :rules="account ? [] : [(v: string) => ACCOUNT_NAME_PATTERN.test(v.trim().toLowerCase()) || t('admin.accountForm.nameInvalid')]"
        :error="Boolean(fieldErrors.name)"
        :error-message="fieldErrors.name"
      />
      <q-input v-model="model.description" outlined dense autogrow maxlength="500" :label="t('admin.accountForm.description')" />
      <RoleSelect v-model="model.roles" :label="t('admin.accountForm.roles')" :hint="t('admin.accountForm.rolesHint')" :keep="account?.roles ?? []" :error="rolesError" />
      <q-toggle v-if="account" v-model="model.disabled" :label="t('admin.accountForm.disabled')" />
    </div>
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call } from '@/api/client'
import type { ServiceAccount } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { t } from '@/i18n'

import { ACCOUNT_NAME_PATTERN, type AccountFormModel, toAccountCreate, toAccountUpdate } from './forms'
import ProblemBanner from './ProblemBanner.vue'
import RoleSelect from './RoleSelect.vue'

const props = defineProps<{ account: ServiceAccount | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [account: ServiceAccount] }>()

const model = ref<AccountFormModel>({ name: '', description: '', roles: [], disabled: false })
const saving = ref(false)
const problem = ref<unknown>(null)
const fieldErrors = ref<Record<string, string>>({})
const rolesError = computed(() => Object.entries(fieldErrors.value).find(([k]) => k.startsWith('roles'))?.[1] ?? null)

watch(open, (isOpen) => {
  if (!isOpen) return
  const a = props.account
  model.value = { name: a?.name ?? '', description: a?.description ?? '', roles: [...(a?.roles ?? [])], disabled: a?.disabled ?? false }
  problem.value = null
  fieldErrors.value = {}
})

async function save(): Promise<void> {
  saving.value = true
  problem.value = null
  fieldErrors.value = {}
  try {
    const a = props.account
    const saved = a
      ? await call(api.PATCH('/service-accounts/{account_id}', { params: { path: { account_id: a.id } }, body: toAccountUpdate(model.value, a) }))
      : await call(api.POST('/service-accounts', { body: toAccountCreate(model.value) }))
    open.value = false
    emit('saved', saved)
  } catch (error) {
    if (error instanceof ApiError) fieldErrors.value = error.fieldErrors
    problem.value = error
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.account-form {
  display: grid;
  gap: 14px;
}
</style>
