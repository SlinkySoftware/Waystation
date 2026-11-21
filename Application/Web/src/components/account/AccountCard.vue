<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Who you are (UI_DESIGN §4.11): username, source, and for local accounts an editable display name and e-mail
     (`PATCH /auth/me`; provider accounts get their profile from the provider at every sign-in). -->
<template>
  <PageCard :title="t('profile.account')">
    <template v-if="local && !editing" #actions>
      <q-btn flat dense no-caps :icon="iconEdit" :label="t('common.edit')" @click="startEdit" />
    </template>
    <q-form v-if="editing" class="account__form" @submit="save">
      <div v-if="formError" class="form-error" role="alert">{{ formError }}</div>
      <q-input v-model="displayName" outlined dense autocomplete="name" :label="t('profile.displayName')" :rules="[(v: string) => Boolean(v?.trim()) || t('profile.password.required')]" :error="Boolean(fieldErrors.display_name)" :error-message="fieldErrors.display_name" />
      <q-input v-model="email" outlined dense type="email" autocomplete="email" :label="t('profile.email')" :hint="t('ui.profile.emailHint')" :error="Boolean(fieldErrors.email)" :error-message="fieldErrors.email" />
      <div class="account__actions">
        <q-btn flat no-caps :label="t('common.cancel')" :disable="saving" @click="editing = false" />
        <q-btn unelevated no-caps color="primary" type="submit" :loading="saving" :label="t('common.save')" />
      </div>
    </q-form>
    <dl v-else class="account__facts">
      <div>
        <dt>{{ t('profile.displayName') }}</dt>
        <dd>{{ user?.display_name }}</dd>
      </div>
      <div>
        <dt>{{ t('profile.username') }}</dt>
        <dd class="mono">{{ user?.username }}</dd>
      </div>
      <div>
        <dt>{{ t('profile.email') }}</dt>
        <dd>{{ user?.email || '–' }}</dd>
      </div>
      <div>
        <dt>{{ t('profile.source') }}</dt>
        <dd>{{ local ? t('profile.sourceLocal') : user?.source }}</dd>
      </div>
    </dl>
    <p v-if="!local" class="account__note">{{ t('ui.profile.providerNote') }}</p>
  </PageCard>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import PageCard from '@/components/PageCard.vue'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { iconEdit } from '@/utils/icons'

const auth = useAuthStore()
const notify = useNotify()
const user = computed(() => auth.user)
const local = computed(() => user.value?.source === 'local')

const editing = ref(false)
const saving = ref(false)
const displayName = ref('')
const email = ref('')
const formError = ref<string | null>(null)
const fieldErrors = ref<Record<string, string>>({})

function startEdit(): void {
  displayName.value = user.value?.display_name ?? ''
  email.value = user.value?.email ?? ''
  formError.value = null
  fieldErrors.value = {}
  editing.value = true
}

async function save(): Promise<void> {
  saving.value = true
  formError.value = null
  fieldErrors.value = {}
  try {
    await call(api.PATCH('/auth/me', { body: { display_name: displayName.value.trim(), email: email.value.trim() || null } }))
    await auth.refresh()
    editing.value = false
    notify.success(t('ui.profile.saved'))
  } catch (error) {
    if (error instanceof ApiError && Object.keys(error.fieldErrors).length) fieldErrors.value = error.fieldErrors
    else formError.value = errorMessage(error)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.account__facts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px 24px;
  margin: 0;
}

.account__facts dt {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.account__facts dd {
  margin: 2px 0 0;
  font-weight: 500;
  overflow-wrap: anywhere;
}

.account__form {
  display: grid;
  gap: 8px;
  max-width: 480px;
}

.account__actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.account__note {
  margin: 12px 0 0;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}
</style>
