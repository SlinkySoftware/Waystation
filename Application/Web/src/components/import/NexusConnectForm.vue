<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Import from Nexus, step 1: where to read from. The password is sent with each preview/apply and never kept. -->
<template>
  <form class="nx-form" autocomplete="off" @submit.prevent="emit('submit')">
    <q-input
      v-model="model.url"
      outlined
      dense
      type="url"
      :label="t('nexusImport.url')"
      :hint="t('nexusImport.urlHint')"
      :error="!!errors.url"
      :error-message="errors.url"
      placeholder="https://nexus.example.com"
      class="nx-form__url"
      autofocus
    />
    <div class="nx-form__row">
      <q-input v-model="model.username" outlined dense :label="t('nexusImport.username')" class="col" :error="!!errors.username" :error-message="errors.username" autocomplete="off" />
      <q-input
        v-model="model.password"
        outlined
        dense
        :type="showPassword ? 'text' : 'password'"
        :label="t('nexusImport.password')"
        class="col"
        :error="!!errors.password"
        :error-message="errors.password"
        autocomplete="new-password"
      >
        <template #append>
          <q-btn flat dense round size="sm" :icon="showPassword ? iconHide : iconShow" :aria-label="t(showPassword ? 'nexusImport.hidePassword' : 'nexusImport.showPassword')" @click="showPassword = !showPassword" />
        </template>
      </q-input>
    </div>
    <p class="nx-form__note"><q-icon :name="iconLock" size="15px" />{{ t('nexusImport.credentialsNote') }}</p>

    <q-expansion-item v-model="advanced" dense dense-toggle switch-toggle-side :label="t('nexusImport.advanced')" header-class="nx-form__adv-head">
      <div class="nx-form__adv">
        <q-toggle v-model="model.allowPrivate" :label="t('nexusImport.allowPrivate')" />
        <div class="nx-form__help">{{ t('nexusImport.allowPrivateHint') }}</div>
        <q-toggle v-model="model.dockerAliases" :label="t('nexusImport.dockerAliases')" />
        <div class="nx-form__help">{{ t('nexusImport.dockerAliasesHint') }}</div>
        <q-select
          v-model="model.defaultDockerRepo"
          outlined
          dense
          clearable
          emit-value
          map-options
          :options="dockerOptions"
          :label="t('nexusImport.defaultDocker')"
          :hint="t('nexusImport.defaultDockerHint')"
          :disable="!dockerOptions.length"
        />
        <q-input
          v-model="model.caPem"
          outlined
          dense
          type="textarea"
          autogrow
          input-class="mono"
          :label="t('nexusImport.caPem')"
          :hint="t('nexusImport.caPemHint')"
          :error="!!errors.ca_pem"
          :error-message="errors.ca_pem"
        />
      </div>
    </q-expansion-item>

    <div class="nx-form__actions">
      <q-btn type="submit" unelevated no-caps color="primary" :icon="iconSearch" :label="t('nexusImport.preview')" :loading="busy" :disable="!ready" />
    </div>
  </form>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { t } from '@/i18n'
import { iconHide, iconLock, iconSearch, iconShow } from '@/utils/icons'

import type { NexusFormModel } from './nexusImport'

const props = defineProps<{ busy: boolean; errors: Record<string, string>; dockerRepos: string[] }>()
const model = defineModel<NexusFormModel>({ required: true })
const emit = defineEmits<{ submit: [] }>()

const showPassword = ref(false)
const advanced = ref(Boolean(model.value.caPem || model.value.allowPrivate))
const ready = computed(() => Boolean(model.value.url.trim() && model.value.username.trim() && model.value.password))
const dockerOptions = computed(() => [{ label: t('nexusImport.defaultDockerAuto'), value: null }, ...props.dockerRepos.map((n) => ({ label: n, value: n }))])
</script>

<style scoped>
.nx-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.nx-form__row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
.nx-form__row > * {
  min-width: 220px;
}
.nx-form__note {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-13);
}
.nx-form__adv {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 0 4px;
}
.nx-form__help {
  margin: -6px 0 4px 42px;
  color: var(--text-muted);
  font-size: var(--fs-12);
}
.nx-form__actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 4px;
}
</style>
