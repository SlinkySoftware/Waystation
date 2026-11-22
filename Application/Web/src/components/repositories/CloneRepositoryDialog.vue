<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Clone a repository: its configuration (never its content) under a new name (SPECIFICATION §4.12, T-S05). -->
<template>
  <FormDialog v-model="open" :title="t('repoClone.title', { name: source.name })" :caption="t('repoClone.caption')" :saving="saving" :submit-label="t('repoClone.submit')" @submit="save">
    <div v-if="formError" class="form-error" role="alert">{{ formError }}</div>
    <div class="clone-form">
      <q-input
        v-model="name"
        outlined
        dense
        autofocus
        :label="t('repoForm.name')"
        :hint="t('repoForm.nameHint', { url: `${baseUrl}/repository/${name || '<name>'}/` })"
        :rules="[(v: string) => NAME_PATTERN.test(v) || t('repoForm.nameInvalid')]"
        :error="!!fieldErrors.name"
        :error-message="fieldErrors.name"
        autocomplete="off"
        spellcheck="false"
        data-test="clone-name"
      />
      <q-input v-model="description" outlined dense :label="t('repoForm.description')" maxlength="500" />
      <q-toggle v-model="online" :label="t('repoForm.online')" />
      <p v-if="dropsUniqueOptions" class="clone-form__note">{{ t('repoClone.dockerNote') }}</p>
    </div>
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { Repository } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useConfigStore } from '@/stores/config'

import { NAME_PATTERN } from './form'

const props = defineProps<{ source: Repository }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ cloned: [repo: Repository] }>()
const notify = useNotify()

const config = useConfigStore()
const baseUrl = computed(() => config.config?.base_url ?? '')
const name = ref('')
const description = ref('')
const online = ref(true)
const saving = ref(false)
const fieldErrors = ref<Record<string, string>>({})
const formError = ref<string | null>(null)

const dropsUniqueOptions = computed(() => {
  const options = (props.source.format_options ?? {}) as { aliases?: unknown[]; default_repo?: boolean; connector_port?: number | null }
  return props.source.format === 'docker' && (!!options.aliases?.length || !!options.default_repo || options.connector_port != null)
})

watch(
  open,
  (isOpen) => {
    if (!isOpen) return
    name.value = `${props.source.name}-copy`.slice(0, 63)
    description.value = props.source.description ?? ''
    online.value = props.source.online
    fieldErrors.value = {}
    formError.value = null
  },
  { immediate: true },
)

async function save(): Promise<void> {
  saving.value = true
  fieldErrors.value = {}
  formError.value = null
  try {
    const repo = await call(
      api.POST('/repositories/{name}/clone', {
        params: { path: { name: props.source.name } },
        body: { name: name.value, description: description.value, online: online.value },
      }),
    )
    notify.success(t('repoClone.done', { name: repo.name }))
    open.value = false
    emit('cloned', repo)
  } catch (error) {
    if (error instanceof ApiError && error.problem.errors?.length) {
      fieldErrors.value = error.fieldErrors
      // Errors on fields this dialog has no input for (copied configuration) are shown as the form error.
      const copied = Object.entries(error.fieldErrors).filter(([field]) => field !== 'name')
      formError.value = copied.length ? copied.map(([field, message]) => `${field}: ${message}`).join(' · ') : (error.problem.detail ?? null)
    } else formError.value = errorMessage(error)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.clone-form {
  display: grid;
  gap: 8px;
}

.clone-form__note {
  margin: 0;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.form-error {
  margin-bottom: 14px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--cache-error-bg);
  color: var(--cache-error);
  font-size: var(--fs-13);
}
</style>
