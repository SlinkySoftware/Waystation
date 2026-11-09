<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Import from Nexus (SPEC §4.12): connect, review the dry-run plan (create / exists / unsupported / invalid with
     field-level differences), pick what to import, apply. Needs `admin`; the server decides. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nexusImport.title')" :caption="t('nexusImport.caption')" :crumbs="[{ label: t('nav.repositories'), to: { name: 'repositories' } }, { label: t('nexusImport.title') }]" />

    <q-card class="nx-page__card">
      <div class="nx-page__card-title">{{ t('nexusImport.connect') }}</div>
      <NexusConnectForm v-model="model" :busy="previewing" :errors="fieldErrors" :docker-repos="dockerRepos" @submit="preview" />
    </q-card>

    <div v-if="error" class="nx-page__error" role="alert"><q-icon :name="iconError" size="18px" />{{ error }}</div>

    <q-card v-if="report" class="nx-page__card nx-page__plan">
      <div class="nx-page__plan-head">
        <div class="col">
          <div class="nx-page__card-title">{{ report.applied ? t('nexusImport.resultTitle') : t('nexusImport.planTitle') }}</div>
          <div class="nx-page__summary" aria-live="polite">
            <span v-if="report.nexus_version" class="mono">{{ report.nexus_version }}</span>
            <span>{{ t('nexusImport.summary.total', { count: report.summary.total }) }}</span>
            <span v-if="report.applied" class="ws-badge tone-ok">{{ t('nexusImport.summary.created', { count: report.summary.created }) }}</span>
            <span v-else class="ws-badge tone-hit">{{ t('nexusImport.summary.create', { count: report.summary.create }) }}</span>
            <span v-if="report.summary.failed" class="ws-badge tone-blocked">{{ t('nexusImport.summary.failed', { count: report.summary.failed }) }}</span>
            <span class="ws-badge tone-offline">{{ t('nexusImport.summary.exists', { count: report.summary.exists }) }}</span>
            <span class="ws-badge tone-offline">{{ t('nexusImport.summary.unsupported', { count: report.summary.unsupported }) }}</span>
            <span v-if="report.summary.invalid" class="ws-badge tone-degraded">{{ t('nexusImport.summary.invalid', { count: report.summary.invalid }) }}</span>
          </div>
          <div v-if="report.default_docker_repo" class="nx-page__docker">
            <q-icon :name="iconInfo" size="16px" class="nx-page__docker-icon" />
            <span>
              {{ report.default_docker_repo.name ? t('nexusImport.defaultDockerIs', { name: report.default_docker_repo.name }) : t('nexusImport.defaultDockerNone') }}
              <span class="text-muted">({{ report.default_docker_repo.reason }})</span>
            </span>
          </div>
        </div>
        <div class="nx-page__plan-actions">
          <q-btn v-if="report.applied" unelevated no-caps color="primary" :label="t('nexusImport.viewRepositories')" :to="{ name: 'repositories' }" />
          <q-btn v-else unelevated no-caps color="primary" :icon="iconDownload" :label="t('nexusImport.apply', { count: selected.length })" :disable="!selected.length" :loading="applying" @click="confirmApply" />
        </div>
      </div>
      <NexusImportTable v-model:selected="selected" :items="report.items" :applied="report.applied" />
    </q-card>
    <EmptyState v-else-if="!previewing" class="nx-page__empty" illustration="repositories" :title="t('nexusImport.emptyTitle')" :text="t('nexusImport.emptyText')" />
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, ref } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { NexusImportReport } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import NexusConnectForm from '@/components/import/NexusConnectForm.vue'
import NexusImportTable from '@/components/import/NexusImportTable.vue'
import { creatable, dockerCandidates, emptyModel, type NexusFormModel, toApply, toRequest } from '@/components/import/nexusImport'
import PageHeader from '@/components/PageHeader.vue'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'
import { iconDownload, iconError, iconInfo } from '@/utils/icons'

const $q = useQuasar()
const notify = useNotify()
const repos = useRepositoriesStore()

const model = ref<NexusFormModel>(emptyModel())
const report = ref<NexusImportReport | null>(null)
const selected = ref<string[]>([])
const previewing = ref(false)
const applying = ref(false)
const error = ref<string | null>(null)
const fieldErrors = ref<Record<string, string>>({})
const dockerRepos = computed(() => dockerCandidates(report.value))

function fail(e: unknown): void {
  if (e instanceof ApiError && e.status === 422 && Object.keys(e.fieldErrors).length) {
    fieldErrors.value = e.fieldErrors
    error.value = null
  } else {
    error.value = errorMessage(e)
  }
}

async function preview(): Promise<void> {
  previewing.value = true
  error.value = null
  fieldErrors.value = {}
  try {
    report.value = await call(api.POST('/import/nexus/preview', { body: toRequest(model.value) }))
    selected.value = creatable(report.value)
  } catch (e) {
    report.value = null
    fail(e)
  } finally {
    previewing.value = false
  }
}

function confirmApply(): void {
  $q.dialog({
    title: t('nexusImport.confirmTitle'),
    message: t('nexusImport.confirmText', { count: selected.value.length }),
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'primary', label: t('nexusImport.apply', { count: selected.value.length }) },
  }).onOk(() => void apply())
}

async function apply(): Promise<void> {
  applying.value = true
  error.value = null
  try {
    const result = await call(api.POST('/import/nexus/apply', { body: toApply(model.value, selected.value) }))
    report.value = result
    model.value.password = '' // never kept longer than needed
    void repos.load(true).catch(() => undefined)
    if (result.summary.failed) notify.error(t('nexusImport.appliedWithFailures', { created: result.summary.created, failed: result.summary.failed }))
    else notify.success(t('nexusImport.applied', { count: result.summary.created }))
  } catch (e) {
    fail(e)
  } finally {
    applying.value = false
  }
}
</script>

<style scoped>
.nx-page__card {
  padding: 16px 20px;
  margin-bottom: 16px;
}
.nx-page__card-title {
  font-weight: 600;
  font-size: var(--fs-16);
  margin-bottom: 8px;
}
.nx-page__plan {
  padding: 0;
  overflow: hidden;
}
.nx-page__plan-head {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: flex-start;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
}
.nx-page__summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  color: var(--text-secondary);
  font-size: var(--fs-13);
}
.nx-page__docker-icon {
  flex: none;
  margin-top: 2px;
}
.nx-page__docker {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-top: 8px;
  font-size: var(--fs-13);
}
.nx-page__error {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  padding: 10px 14px;
  border-radius: var(--radius);
  color: var(--status-blocked);
  background: var(--status-blocked-bg);
}
.nx-page__empty {
  margin-top: 24px;
}
</style>
