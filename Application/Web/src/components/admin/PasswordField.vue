<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A new password for someone else: show/hide, generate a strong one, copy it. Rules: 12+ characters, not the
     username (the server checks again). -->
<template>
  <q-input
    v-model="model"
    outlined
    dense
    :type="visible ? 'text' : 'password'"
    maxlength="1024"
    autocomplete="new-password"
    :input-class="visible ? 'mono' : undefined"
    :label="t('admin.userForm.password')"
    :hint="t('admin.userForm.passwordHint')"
    :rules="[rule]"
    :error="Boolean(serverError)"
    :error-message="serverError ?? undefined"
  >
    <template #append>
      <q-btn flat round dense size="sm" :icon="visible ? iconHide : iconShow" :aria-label="visible ? t('signIn.hidePassword') : t('signIn.showPassword')" @click="visible = !visible" />
      <q-btn v-if="model" flat round dense size="sm" :icon="iconCopy" :aria-label="t('common.copy')" @click="copy(model, t('admin.userForm.password'))" />
    </template>
    <template #after>
      <q-btn flat no-caps dense :icon="iconRefresh" :label="t('admin.userForm.generate')" @click="generate" />
    </template>
  </q-input>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import { useCopy } from '@/composables/useNotify'
import { t } from '@/i18n'
import { iconCopy, iconHide, iconRefresh, iconShow } from '@/utils/icons'

import { generatePassword, passwordProblem } from './forms'

const props = withDefaults(defineProps<{ username: string; serverError?: string | null }>(), { serverError: null })
const model = defineModel<string>({ required: true })
const visible = ref(false)
const copy = useCopy()

function rule(value: string): true | string {
  const problem = passwordProblem(value ?? '', props.username)
  if (problem === 'short') return t('admin.userForm.passwordShort')
  if (problem === 'username') return t('admin.userForm.passwordUsername')
  return true
}

function generate(): void {
  model.value = generatePassword()
  visible.value = true
}
</script>
