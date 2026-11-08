<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Create / edit a sign-in provider (LDAP directory or OpenID Connect): sectioned form, tests, group mapping
     (UI_DESIGN §4.10). The kind is picked on create and cannot change afterwards. -->
<template>
  <FormDialog v-model="open" :title="title" :caption="isOidc ? t('oidcForm.caption') : t('ldapForm.caption')" :saving="saving" width="780px" @submit="save">
    <div v-if="formError" class="pdialog__error" role="alert">{{ formError }}</div>
    <div class="pdialog">
      <div v-if="!provider" class="pdialog__kind">
        <span class="text-caption">{{ t('oidcForm.kind') }}</span>
        <q-btn-toggle v-model="model.write.kind" no-caps unelevated dense toggle-color="primary" class="pdialog__kind-toggle" :options="kindOptions" />
      </div>
      <div v-if="isOidc" class="pdialog__presets">
        <span class="text-caption">{{ t('oidcForm.presets') }}</span>
        <q-chip v-for="p in OIDC_PRESET_IDS" :key="p" clickable outline :label="t(`oidcForm.preset.${p}`)" class="pdialog__preset" @click="applyOidcPreset(model, p)" />
        <q-icon :name="iconInfo" size="16px" class="text-muted">
          <q-tooltip>{{ t('oidcForm.presetHint') }}</q-tooltip>
        </q-icon>
      </div>
      <div v-else class="pdialog__presets">
        <span class="text-caption">{{ t('ldapForm.presets') }}</span>
        <q-chip v-for="p in PRESET_IDS" :key="p" clickable outline :label="t(`ldapForm.preset.${p}`)" class="pdialog__preset" @click="applyPreset(model, p)" />
        <q-icon :name="iconInfo" size="16px" class="text-muted">
          <q-tooltip>{{ t('ldapForm.presetHint') }}</q-tooltip>
        </q-icon>
      </div>

      <section class="pdialog__section">
        <h3 class="section-label">{{ t('ldapForm.general') }}</h3>
        <div class="pdialog__row">
          <q-input
            v-model="model.write.name"
            outlined
            dense
            class="col"
            maxlength="64"
            :label="t('ldapForm.name')"
            :hint="t('ldapForm.nameHint')"
            :rules="[(v: string) => !!v.trim() || t('ldapForm.required')]"
            :error="!!fieldErrors.name"
            :error-message="fieldErrors.name"
            autocomplete="off"
          />
          <q-input
            v-model.number="model.write.order"
            outlined
            dense
            type="number"
            min="0"
            max="1000"
            class="pdialog__order"
            :label="t('ldapForm.order')"
            :hint="t('ldapForm.orderHint')"
            :error="!!fieldErrors.order"
            :error-message="fieldErrors.order"
          />
        </div>
        <q-toggle v-model="model.write.enabled" :label="t('ldapForm.enabled')" />
      </section>

      <OidcSection v-if="isOidc" v-model="model" :errors="fieldErrors" />
      <template v-else>
        <LdapConnectionSection v-model="model" :errors="fieldErrors" />
        <LdapDirectorySection v-model="model" :errors="fieldErrors" />
      </template>
      <ProviderTests :model="model" :provider-id="provider?.id ?? null" />
      <GroupMappingTable v-model="model.write.group_mapping" :errors="fieldErrors" :kind="model.write.kind" />
    </div>
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { AuthProviderConfig } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { iconInfo } from '@/utils/icons'

import GroupMappingTable from './GroupMappingTable.vue'
import LdapConnectionSection from './LdapConnectionSection.vue'
import LdapDirectorySection from './LdapDirectorySection.vue'
import OidcSection from './OidcSection.vue'
import ProviderTests from './ProviderTests.vue'
import { applyOidcPreset, applyPreset, emptyProvider, fromConfig, OIDC_PRESET_IDS, PRESET_IDS, type ProviderFormModel, toWrite } from './providerForm'

const props = defineProps<{ provider: AuthProviderConfig | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [provider: AuthProviderConfig] }>()

const notify = useNotify()
const model = ref<ProviderFormModel>(emptyProvider())
const saving = ref(false)
const fieldErrors = ref<Record<string, string>>({})
const formError = ref<string | null>(null)

watch(open, (isOpen) => {
  if (!isOpen) return
  model.value = props.provider ? fromConfig(props.provider) : emptyProvider()
  fieldErrors.value = {}
  formError.value = null
})

const title = computed(() => (props.provider ? t('ldapForm.editTitle', { name: props.provider.name }) : t('oidcForm.createTitle')))
const isOidc = computed(() => model.value.write.kind === 'oidc')
const kindOptions = (['ldap', 'oidc'] as const).map((value) => ({ value, label: t(`oidcForm.kindOption.${value}`) }))

async function save(): Promise<void> {
  saving.value = true
  fieldErrors.value = {}
  formError.value = null
  try {
    const body = toWrite(model.value)
    const saved = props.provider
      ? await call(api.PUT('/auth-providers/{id}', { params: { path: { id: props.provider.id } }, body }))
      : await call(api.POST('/auth-providers', { body }))
    notify.success(props.provider ? t('authProviders.saved', { name: saved.name }) : t('authProviders.created', { name: saved.name }))
    open.value = false
    emit('saved', saved)
  } catch (error) {
    if (error instanceof ApiError && error.problem.errors?.length) {
      fieldErrors.value = error.fieldErrors
      formError.value = error.problem.detail ?? null
    } else formError.value = errorMessage(error)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.pdialog {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.pdialog__presets {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 6px;
}

.pdialog__preset {
  margin: 0;
}

.pdialog__kind {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 12px;
}

.pdialog__kind-toggle {
  border: 1px solid var(--border-strong);
}

.pdialog__section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.pdialog__section h3 {
  margin: 0;
}

.pdialog__row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.pdialog__row > .col {
  min-width: 220px;
}

.pdialog__order {
  width: 170px;
}

.pdialog__error {
  margin-bottom: 14px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--cache-error-bg);
  color: var(--cache-error);
  font-size: var(--fs-13);
}
</style>
