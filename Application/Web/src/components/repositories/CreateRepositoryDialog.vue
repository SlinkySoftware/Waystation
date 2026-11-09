<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Create repository: format tiles → type → sectioned form (UI_DESIGN §4.3). -->
<template>
  <FormDialog v-model="open" :title="title" :caption="caption" :saving="saving" :readonly="step !== 'form'" :submit-label="t('repoCreate.create')" width="680px" @submit="save">
    <template v-if="step !== 'format'" #footer-start>
      <q-btn flat no-caps :icon="iconBack" :label="t('common.back')" :disable="saving" @click="back" />
    </template>

    <div v-if="step === 'format'" class="tiles">
      <button
        v-for="f in FORMATS"
        :key="f.format"
        type="button"
        class="tile"
        :class="{ 'tile--soon': !f.available }"
        :disabled="!f.available"
        :aria-label="formatLabel(f.format)"
        @click="pickFormat(f.format as M0Format)"
      >
        <FormatIcon :mark="formatMark(f.format)" :size="30" :brand="f.available" decorative />
        <span class="tile__name">{{ formatLabel(f.format) }}</span>
        <span class="tile__sub">{{ f.available ? t(`repoCreate.blurb.${f.format as M0Format}`) : t('repoCreate.phase', { phase: f.phase ?? '' }) }}</span>
      </button>
    </div>

    <div v-else-if="step === 'type'" class="tiles tiles--types">
      <button v-for="ty in TYPES" :key="ty" type="button" class="tile tile--type" :class="{ 'tile--soon': !typeAvailable(ty) }" :disabled="!typeAvailable(ty)" @click="pickType(ty)">
        <q-icon :name="typeMeta(ty).icon" size="28px" class="tile__icon" />
        <span class="tile__name">{{ typeMeta(ty).label }}</span>
        <span class="tile__sub">{{ t(`repoCreate.type.${ty}`) }}</span>
        <span v-if="!typeAvailable(ty)" class="tile__soon">{{ t('repoCreate.notInM0') }}</span>
      </button>
    </div>

    <template v-else>
      <div v-if="formError" class="form-error" role="alert">{{ formError }}</div>
      <RepositoryForm v-model="model" mode="create" :errors="fieldErrors" />
    </template>
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { Format, M0Format, Repository, RepositoryType } from '@/api/types'
import FormatIcon from '@/components/FormatIcon.vue'
import FormDialog from '@/components/FormDialog.vue'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { formatLabel, formatMark, typeMeta } from '@/utils/domain'
import { iconBack } from '@/utils/icons'

import { emptyModel, type FormType, GROUP_FORMATS, HOSTED_FORMATS, type RepoFormModel, toWrite } from './form'
import RepositoryForm from './RepositoryForm.vue'

const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ created: [repo: Repository] }>()

const FORMATS: { format: Format; available: boolean; phase?: number }[] = [
  { format: 'docker', available: true },
  { format: 'npm', available: true },
  { format: 'apt', available: true },
  { format: 'raw', available: true },
  { format: 'maven', available: true },
  { format: 'pypi', available: true },
  { format: 'yum', available: false, phase: 2 },
  { format: 'go', available: false, phase: 5 },
  { format: 'helm', available: false, phase: 5 },
  { format: 'terraform', available: false, phase: 5 },
  { format: 'galaxy', available: false, phase: 5 },
  { format: 'swift', available: false, phase: 5 },
]
const TYPES: RepositoryType[] = ['proxy', 'hosted', 'group']

const notify = useNotify()
const step = ref<'format' | 'type' | 'form'>('format')
const model = ref<RepoFormModel>(emptyModel('docker'))
const saving = ref(false)
const fieldErrors = ref<Record<string, string>>({})
const formError = ref<string | null>(null)

watch(open, (isOpen) => {
  if (isOpen) {
    step.value = 'format'
    fieldErrors.value = {}
    formError.value = null
  }
})

const title = computed(() => (step.value === 'form' ? t('repoCreate.formTitle', { format: formatLabel(model.value.format) }) : t('repoCreate.title')))
const caption = computed(() =>
  step.value === 'format' ? t('repoCreate.step1') : step.value === 'type' ? t('repoCreate.step2', { format: formatLabel(model.value.format) }) : t('repoCreate.step3'),
)

function pickFormat(format: M0Format): void {
  model.value = emptyModel(format)
  step.value = 'type'
}

function typeAvailable(type: RepositoryType): boolean {
  if (type === 'hosted') return HOSTED_FORMATS.includes(model.value.format)
  if (type === 'group') return GROUP_FORMATS.includes(model.value.format)
  return true
}

function pickType(type: RepositoryType): void {
  model.value = emptyModel(model.value.format, type as FormType)
  step.value = 'form'
}

function back(): void {
  step.value = step.value === 'form' ? 'type' : 'format'
}

async function save(): Promise<void> {
  saving.value = true
  fieldErrors.value = {}
  formError.value = null
  try {
    const repo = await call(api.POST('/repositories', { body: toWrite(model.value) }))
    notify.success(t('repoCreate.created', { name: repo.name }))
    open.value = false
    emit('created', repo)
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
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
}

.tiles--types {
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
}

.tile {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--text-primary);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color var(--dur) var(--ease-out), background var(--dur) var(--ease-out), transform var(--dur) var(--ease-out);
}

.tile:hover:not(:disabled) {
  border-color: var(--q-primary);
  background: var(--selected-bg);
  transform: translateY(-1px);
}

.tile:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.tile__icon {
  color: var(--q-primary);
}

.tile__name {
  font-weight: 650;
}

.tile__sub {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  line-height: 1.35;
}

.tile__soon {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
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
