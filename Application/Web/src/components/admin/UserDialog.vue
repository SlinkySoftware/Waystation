<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Create a local user, or edit a user's profile (local accounts), roles and local groups. Delegation (403) and
     last-administrator (409) refusals are shown above the form; field errors next to their field. -->
<template>
  <FormDialog
    v-model="open"
    :title="user ? t('admin.userForm.editTitle', { name: user.username }) : t('admin.userForm.createTitle')"
    :caption="user ? t('admin.userForm.editCaption') : t('admin.userForm.createCaption')"
    :saving="saving"
    :submit-label="user ? t('common.save') : t('admin.users.create')"
    width="620px"
    @submit="save"
  >
    <ProblemBanner :error="problem" />
    <div v-if="external" class="user-form__note" role="note">
      <q-icon :name="iconInfo" size="18px" />
      {{ t('admin.userForm.externalNote', { provider: directory.providerName(user!.source) ?? user!.source }) }}
    </div>
    <div class="user-form">
      <q-input
        v-if="!user"
        v-model="model.username"
        outlined
        dense
        autofocus
        maxlength="128"
        autocomplete="off"
        :label="t('admin.userForm.username')"
        :hint="t('admin.userForm.usernameHint')"
        :rules="[(v: string) => USERNAME.test(v.trim().toLowerCase()) || t('admin.userForm.usernameInvalid')]"
        :error="Boolean(fieldErrors.username)"
        :error-message="fieldErrors.username"
      />
      <div class="user-form__row">
        <q-input v-model="model.display_name" outlined dense maxlength="256" :readonly="external" :label="t('admin.userForm.displayName')" :error="Boolean(fieldErrors.display_name)" :error-message="fieldErrors.display_name" />
        <q-input
          v-model="model.email"
          outlined
          dense
          type="email"
          maxlength="256"
          :readonly="external"
          :label="t('admin.userForm.email')"
          :rules="[(v: string) => !v.trim() || EMAIL.test(v.trim()) || t('admin.userForm.emailInvalid')]"
          :error="Boolean(fieldErrors.email)"
          :error-message="fieldErrors.email"
        />
      </div>

      <template v-if="!user">
        <q-toggle v-model="model.withPassword" :label="t('admin.userForm.setPassword')" />
        <div v-if="!model.withPassword" class="user-form__help">{{ t('admin.userForm.setPasswordHelp') }}</div>
        <template v-else>
          <PasswordField v-model="model.password" :username="model.username" :server-error="fieldErrors.password" />
          <q-checkbox v-model="model.mustChange" dense :label="t('admin.userForm.mustChange')" />
        </template>
      </template>

      <RoleSelect v-model="model.roles" :label="t('admin.userForm.roles')" :hint="t('admin.userForm.rolesHint')" :keep="user?.roles ?? []" :error="listError('roles')" />
      <GroupSelect v-model="model.groups" :label="t('admin.userForm.groups')" :hint="t('admin.userForm.groupsHint')" :error="listError('groups')" />
    </div>
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call } from '@/api/client'
import type { UserDetail } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { t } from '@/i18n'
import { useDirectoryStore } from '@/stores/directory'
import { iconInfo } from '@/utils/icons'

import { emptyUserModel, modelFromUser, toUserCreate, toUserUpdate, type UserFormModel } from './forms'
import GroupSelect from './GroupSelect.vue'
import PasswordField from './PasswordField.vue'
import ProblemBanner from './ProblemBanner.vue'
import RoleSelect from './RoleSelect.vue'

const props = defineProps<{ user: UserDetail | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [user: UserDetail] }>()

const USERNAME = /^[a-z0-9][a-z0-9._@-]{0,127}$/
const EMAIL = /^[^@\s]{1,128}@[^@\s]{1,127}$/
const directory = useDirectoryStore()

const model = ref<UserFormModel>(emptyUserModel())
const saving = ref(false)
const problem = ref<unknown>(null)
const fieldErrors = ref<Record<string, string>>({})
const external = computed(() => Boolean(props.user && props.user.source !== 'local'))

watch(open, (isOpen) => {
  if (!isOpen) return
  model.value = props.user ? modelFromUser(props.user) : emptyUserModel()
  problem.value = null
  fieldErrors.value = {}
})

/** First server error for a list field (`roles.2` → roles). */
function listError(field: string): string | null {
  return Object.entries(fieldErrors.value).find(([k]) => k === field || k.startsWith(`${field}.`))?.[1] ?? null
}

async function save(): Promise<void> {
  saving.value = true
  problem.value = null
  fieldErrors.value = {}
  try {
    const user = props.user
    const saved = user
      ? await call(api.PATCH('/users/{user_id}', { params: { path: { user_id: user.id } }, body: toUserUpdate(model.value, user) }))
      : await call(api.POST('/users', { body: toUserCreate(model.value) }))
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
.user-form {
  display: grid;
  gap: 14px;
}

.user-form__row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

@media (max-width: 599px) {
  .user-form__row {
    grid-template-columns: 1fr;
  }
}

.user-form__help {
  margin-top: -10px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.user-form__note {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin-bottom: 14px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
  font-size: var(--fs-13);
  color: var(--text-secondary);
}
</style>
