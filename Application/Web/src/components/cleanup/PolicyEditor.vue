<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Create or edit a cleanup policy on one screen: the form on the left, the live impact on the right (updates as
     criteria change, ~400 ms debounce), the full candidate list one click away. Field errors from problem+json. -->
<template>
  <FormDialog
    v-model="open"
    :title="policy ? t('cleanup.editor.editTitle', { name: policy.name }) : t('cleanup.editor.createTitle')"
    :caption="t('cleanup.editor.caption')"
    :saving="saving"
    :submit-label="policy ? t('common.save') : t('cleanup.editor.create')"
    width="1060px"
    @submit="save"
  >
    <div v-if="formError" class="editor__error" role="alert">{{ formError }}</div>
    <div class="editor">
      <div class="editor__form">
        <div class="editor__row">
          <q-input
            v-model="name"
            outlined
            dense
            autofocus
            maxlength="63"
            class="editor__name"
            :label="t('cleanup.editor.name')"
            :hint="t('cleanup.editor.nameHint')"
            :rules="[(v: string) => Boolean(v?.trim()) || t('cleanup.editor.nameRequired')]"
            :error="Boolean(errors.name)"
            :error-message="errors.name"
          />
          <q-select v-model="format" outlined dense emit-value map-options :options="formatOptions" :label="t('cleanup.editor.format')" :hint="t('cleanup.editor.formatHint')" :error="Boolean(errors.format)" :error-message="errors.format" class="editor__format">
            <template #option="{ opt, itemProps }">
              <q-item v-bind="itemProps" dense>
                <q-item-section avatar class="editor__opt-icon">
                  <FormatIcon v-if="opt.value !== '*'" :mark="formatMark(opt.value)" :size="18" decorative />
                  <q-icon v-else :name="iconLayers" size="18px" />
                </q-item-section>
                <q-item-section>{{ opt.label }}</q-item-section>
              </q-item>
            </template>
          </q-select>
        </div>
        <q-input v-model="description" outlined dense autogrow maxlength="500" :label="t('cleanup.editor.description')" :error="Boolean(errors.description)" :error-message="errors.description" />

        <section class="editor__section">
          <div class="editor__section-title">{{ t('cleanup.editor.criteria') }}</div>
          <div class="editor__section-caption">{{ t('cleanup.editor.criteriaCaption') }}</div>
          <CriteriaBuilder v-model="criteria" :format="format" :errors="errors" />
          <div v-if="triedSubmit && !hasSelector(criteria)" class="editor__need" role="alert">{{ t('cleanup.editor.needSelector') }}</div>
        </section>

        <section class="editor__section">
          <ScheduleField v-model="schedule" v-model:custom="customSchedule" :error="errors.schedule" />
          <q-toggle v-model="enabled" :label="t('cleanup.editor.enabled')" class="editor__enabled" />
          <div class="editor__hint">{{ t('cleanup.editor.enabledHint') }}</div>
        </section>
      </div>

      <ImpactPanel
        class="editor__impact"
        :totals="impact.totals.value"
        :loading="impact.loading.value"
        :ready="hasSelector(criteria)"
        :error="invalid ? new Error(t('cleanup.impact.fixErrors')) : impact.error.value"
        :scope="scopeLabel"
        :grace-hours="graceHours"
        @review="reviewing = true"
      />
    </div>
    <PreviewDialog v-model="reviewing" :request="previewRequest" :title="policy ? t('cleanup.preview.title', { name: name || policy.name }) : t('cleanup.preview.draftTitle')" />
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { CleanupCriteria, CleanupFormat, CleanupPolicy, CleanupPolicyWrite } from '@/api/types'
import FormatIcon from '@/components/FormatIcon.vue'
import FormDialog from '@/components/FormDialog.vue'
import { type PreviewScope, useCleanupPreview } from '@/composables/useCleanupPreview'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { formatMark, LISTED_FORMATS } from '@/utils/domain'
import { iconLayers } from '@/utils/icons'

import { emptyCriteria, formatScope, hasSelector, isValidRegex, normaliseCriteria, NUMBER_CRITERIA, NUMBER_LIMITS, REGEX_CRITERIA, scheduleMode } from './cleanup'
import CriteriaBuilder from './CriteriaBuilder.vue'
import ImpactPanel from './ImpactPanel.vue'
import PreviewDialog from './PreviewDialog.vue'
import ScheduleField from './ScheduleField.vue'

const props = withDefaults(defineProps<{ policy: CleanupPolicy | null; graceHours?: number | null }>(), { graceHours: null })
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [policy: CleanupPolicy] }>()
const notify = useNotify()

const name = ref('')
const description = ref('')
const format = ref<CleanupFormat>('docker')
const criteria = ref<CleanupCriteria>(emptyCriteria())
const schedule = ref<string | null>(null)
const customSchedule = ref(false)
const enabled = ref(true)
const saving = ref(false)
const triedSubmit = ref(false)
const formError = ref<string | null>(null)
const saveErrors = ref<Record<string, string>>({})
const reviewing = ref(false)

watch(open, (isOpen) => {
  if (!isOpen) return
  const p = props.policy
  name.value = p?.name ?? ''
  description.value = p?.description ?? ''
  format.value = p?.format ?? 'docker'
  criteria.value = p ? { ...emptyCriteria(), ...p.criteria } : { ...emptyCriteria(), last_downloaded_older_than_days: NUMBER_LIMITS.last_downloaded_older_than_days.initial }
  schedule.value = p ? p.schedule : '0 3 * * *'
  customSchedule.value = scheduleMode(schedule.value) === 'custom'
  enabled.value = p?.enabled ?? true
  saveErrors.value = {}
  formError.value = null
  triedSubmit.value = false
})

const formatOptions = computed(() => {
  const list: CleanupFormat[] = ['*', ...LISTED_FORMATS]
  if (props.policy && !list.includes(props.policy.format)) list.push(props.policy.format)
  return list.map((value) => ({ value, label: formatScope(value) }))
})

/** Numbers in range and patterns that compile: otherwise the live preview waits instead of asking the server. */
const invalid = computed(() => {
  const c = criteria.value
  const badNumber = NUMBER_CRITERIA.some((k) => c[k] != null && !(Number.isInteger(c[k]) && c[k]! >= 1 && c[k]! <= NUMBER_LIMITS[k].max))
  return badNumber || REGEX_CRITERIA.some((k) => !isValidRegex(c[k]))
})

// Attached repositories while the format is unchanged; otherwise (and for new or unattached policies) every
// repository of the format, which is what attaching it everywhere would remove.
const useAttached = computed(() => Boolean(props.policy?.repositories.length) && props.policy?.format === format.value)
const previewRequest = computed<PreviewScope | null>(() => {
  if (invalid.value) return null
  return { format: format.value, criteria: normaliseCriteria(criteria.value, format.value), policy_id: useAttached.value ? props.policy!.id : null }
})
const impact = useCleanupPreview(() => (open.value ? previewRequest.value : null))
const scopeLabel = computed(() =>
  useAttached.value
    ? t('cleanup.impact.scopeAttached', { count: props.policy!.repositories.length })
    : t('cleanup.impact.scopeAll', { format: format.value === '*' ? t('cleanup.allFormats').toLowerCase() : formatScope(format.value) }),
)

const errors = computed<Record<string, string>>(() => {
  const live = Object.fromEntries(Object.entries(impact.fieldErrors.value).filter(([k]) => k.startsWith('criteria')))
  return { ...live, ...saveErrors.value }
})

async function save(): Promise<void> {
  triedSubmit.value = true
  if (!hasSelector(criteria.value) || invalid.value) return
  saving.value = true
  formError.value = null
  saveErrors.value = {}
  const body: CleanupPolicyWrite = {
    name: name.value.trim(),
    description: description.value.trim(),
    format: format.value,
    criteria: normaliseCriteria(criteria.value, format.value),
    schedule: schedule.value?.trim() || null,
    enabled: enabled.value,
  }
  try {
    const saved = props.policy
      ? await call(api.PUT('/cleanup-policies/{policy_id}', { params: { path: { policy_id: props.policy.id } }, body }))
      : await call(api.POST('/cleanup-policies', { body }))
    notify.success(props.policy ? t('cleanup.editor.saved', { name: saved.name }) : t('cleanup.editor.created', { name: saved.name }))
    open.value = false
    emit('saved', saved)
  } catch (error) {
    if (error instanceof ApiError && error.problem.errors?.length) saveErrors.value = error.fieldErrors
    else formError.value = errorMessage(error)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.editor {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 20px;
  align-items: start;
}

.editor__form {
  display: grid;
  gap: 12px;
  min-width: 0;
}

.editor__row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 200px;
  gap: 12px;
}

.editor__opt-icon {
  min-width: 30px;
}

.editor__section {
  display: grid;
  gap: 8px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}

.editor__section-title {
  font-size: var(--fs-14);
  font-weight: 650;
}

.editor__section-caption,
.editor__hint {
  margin-top: -4px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.editor__enabled {
  margin-top: 6px;
}

.editor__need {
  font-size: var(--fs-13);
  color: var(--status-blocked);
}

.editor__impact {
  position: sticky;
  top: 0;
}

.editor__error {
  margin-bottom: 14px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--cache-error-bg);
  color: var(--cache-error);
  font-size: var(--fs-13);
}

@media (max-width: 899px) {
  .editor {
    grid-template-columns: 1fr;
  }

  .editor__impact {
    position: static;
  }
}

@media (max-width: 599px) {
  .editor__row {
    grid-template-columns: 1fr;
  }
}
</style>
