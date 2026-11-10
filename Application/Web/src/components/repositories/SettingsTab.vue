<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Repository settings: the same sectioned form as create (name/format/type fixed), plus delete. -->
<template>
  <div class="settings">
    <PageCard :title="t('detail.settings.title')" :caption="managed ? t('provisioning.managedHelp') : t('detail.settings.caption')">
      <template v-if="managed" #actions><ManagedBadge :item="repo" /></template>
      <q-form greedy @submit="save">
        <div v-if="formError" class="settings__error" role="alert">{{ formError }}</div>
        <RepositoryForm v-model="model" mode="edit" :errors="fieldErrors" />
        <div class="settings__actions">
          <q-btn flat no-caps :label="t('detail.settings.reset')" :disable="saving || !dirty" @click="reset" />
          <q-btn unelevated no-caps color="primary" type="submit" :label="t('common.save')" :loading="saving" :disable="!dirty || managed" />
        </div>
      </q-form>
    </PageCard>

    <PageCard v-if="!managed" :title="t('detail.settings.danger')" class="settings__danger">
      <div class="settings__danger-row">
        <div>
          <div class="text-weight-medium">{{ t('detail.settings.deleteTitle') }}</div>
          <div class="text-caption">{{ t('detail.settings.deleteText') }}</div>
        </div>
        <q-btn outline no-caps color="negative" :icon="iconDelete" :label="t('detail.settings.delete')" @click="confirmDelete" />
      </div>
    </PageCard>
  </div>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { Repository } from '@/api/types'
import ManagedBadge from '@/components/ManagedBadge.vue'
import PageCard from '@/components/PageCard.vue'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'
import { iconDelete } from '@/utils/icons'
import { isManaged } from '@/utils/managed'

import { modelFromRepository, type RepoFormModel, toWrite } from './form'
import RepositoryForm from './RepositoryForm.vue'

const props = defineProps<{ repo: Repository }>()
const emit = defineEmits<{ saved: [repo: Repository] }>()
const managed = computed(() => isManaged(props.repo))

const $q = useQuasar()
const router = useRouter()
const notify = useNotify()
const repos = useRepositoriesStore()

const model = ref<RepoFormModel>(modelFromRepository(props.repo))
const baseline = ref(JSON.stringify(model.value))
const saving = ref(false)
const fieldErrors = ref<Record<string, string>>({})
const formError = ref<string | null>(null)
const dirty = computed(() => JSON.stringify(model.value) !== baseline.value)

watch(
  () => props.repo,
  (next) => {
    model.value = modelFromRepository(next)
    baseline.value = JSON.stringify(model.value)
  },
)

function reset(): void {
  model.value = JSON.parse(baseline.value) as RepoFormModel
  fieldErrors.value = {}
  formError.value = null
}

async function save(): Promise<void> {
  saving.value = true
  fieldErrors.value = {}
  formError.value = null
  try {
    const next = await call(api.PUT('/repositories/{name}', { params: { path: { name: props.repo.name } }, body: toWrite(model.value) }))
    notify.success(t('detail.settings.saved'))
    emit('saved', next)
  } catch (error) {
    if (error instanceof ApiError && error.problem.errors?.length) {
      fieldErrors.value = error.fieldErrors
      formError.value = error.problem.detail ?? null
    } else formError.value = errorMessage(error)
  } finally {
    saving.value = false
  }
}

function confirmDelete(): void {
  $q.dialog({
    title: t('detail.settings.deleteConfirmTitle', { name: props.repo.name }),
    message: t('detail.settings.deleteConfirmText'),
    prompt: { model: '', type: 'text', isValid: (v: string) => v === props.repo.name, label: t('detail.settings.typeName', { name: props.repo.name }) },
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'negative', label: t('detail.settings.delete') },
    persistent: true,
  }).onOk(async () => {
    try {
      await call(api.DELETE('/repositories/{name}', { params: { path: { name: props.repo.name } } }))
      repos.remove(props.repo.name)
      notify.success(t('detail.settings.deleted', { name: props.repo.name }))
      await router.replace({ name: 'repositories' })
    } catch (error) {
      notify.error(error)
    }
  })
}
</script>

<style scoped>
.settings {
  display: grid;
  gap: 12px;
  max-width: 820px;
}

.settings__error {
  margin-bottom: 14px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--cache-error-bg);
  color: var(--cache-error);
}

.settings__actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

.settings__danger {
  border-color: color-mix(in srgb, var(--cache-error) 40%, transparent);
}

.settings__danger-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}

@media (max-width: 599px) {
  .settings__actions {
    position: sticky;
    bottom: 0;
    background: var(--surface);
    padding-bottom: 8px;
  }
}
</style>
