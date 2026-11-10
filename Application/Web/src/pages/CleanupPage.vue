<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Cleanup (UI_DESIGN §4.8): policies with their criteria, schedule, attachments and last run; the editor with a
     live preview; run history; and blob garbage collection status (§4.7). `?edit=<id>` / `?new=1` open the editor. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.cleanup')" :caption="t('cleanup.caption')">
      <template #actions>
        <q-btn v-if="isAdmin" unelevated no-caps color="primary" :icon="iconAdd" :label="t('cleanup.newPolicy')" @click="edit(null)" />
      </template>
    </PageHeader>

    <div class="cleanup">
      <PageCard :title="t('cleanup.policies')" :caption="t('cleanup.policiesCaption')" flush>
        <LoadFailedState v-if="list.error.value && !list.data.value" :error="list.error.value" :title="t('cleanup.loadFailed')" :loading="list.loading.value" @retry="list.reload" />
        <PolicyTable
          v-else
          :policies="policies"
          :loading="list.loading.value && !list.data.value"
          :can-edit="isAdmin"
          @edit="edit"
          @run="run"
          @history="showHistory"
          @delete="confirmDelete"
          @toggle="toggle"
        >
          <template #empty>
            <EmptyState class="full-width" illustration="cache" :title="t('cleanup.emptyTitle')" :text="t('cleanup.emptyText')">
              <q-btn v-if="isAdmin" unelevated no-caps color="primary" :icon="iconAdd" :label="t('cleanup.newPolicy')" @click="edit(null)" />
            </EmptyState>
          </template>
        </PolicyTable>
      </PageCard>

      <GcCard v-if="isAdmin" :can-run="isAdmin" @grace="(h: number) => (graceHours = h)" />
    </div>

    <PolicyEditor v-model="editorOpen" :policy="editing" :grace-hours="graceHours" @saved="onSaved" />
    <RunHistoryDialog v-model="historyOpen" :policy="historyFor" />
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, call } from '@/api/client'
import type { CleanupPolicy } from '@/api/types'
import { toWrite } from '@/components/cleanup/cleanup'
import GcCard from '@/components/cleanup/GcCard.vue'
import PolicyEditor from '@/components/cleanup/PolicyEditor.vue'
import PolicyTable from '@/components/cleanup/PolicyTable.vue'
import RunHistoryDialog from '@/components/cleanup/RunHistoryDialog.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadFailedState from '@/components/LoadFailedState.vue'
import PageCard from '@/components/PageCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { usePolling } from '@/composables/usePolling'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useRepositoriesStore } from '@/stores/repositories'
import { iconAdd } from '@/utils/icons'

const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const notify = useNotify()
const auth = useAuthStore()
const isAdmin = computed(() => auth.can('admin'))

const list = useAsync(() => call(api.GET('/cleanup-policies')))
const policies = computed(() => list.data.value?.items ?? [])
void useRepositoriesStore().load().catch(() => undefined)
usePolling(() => policies.value.some((p) => p.last_run?.status === 'running'), () => list.reload())

const editorOpen = ref(false)
const editing = ref<CleanupPolicy | null>(null)
const historyOpen = ref(false)
const historyFor = ref<CleanupPolicy | null>(null)
const graceHours = ref<number | null>(null)

function edit(policy: CleanupPolicy | null): void {
  editing.value = policy
  editorOpen.value = true
}

// Deep links from elsewhere (the repository Cleanup tab): open the editor once the list has loaded.
watch(
  () => [list.data.value, route.query.edit, route.query.new] as const,
  ([data, id, isNew]) => {
    if (!data || !isAdmin.value) return
    const target = typeof id === 'string' ? data.items.find((p) => p.id === id) : undefined
    if (target || isNew === '1') {
      edit(target ?? null)
      void router.replace({ query: {} })
    }
  },
  { immediate: true },
)

function onSaved(): void {
  void list.reload()
}

function showHistory(policy: CleanupPolicy): void {
  historyFor.value = policy
  historyOpen.value = true
}

async function run(policy: CleanupPolicy): Promise<void> {
  try {
    const job = await call(api.POST('/cleanup-policies/{policy_id}/run', { params: { path: { policy_id: policy.id } } }))
    notify.success(job.status === 'merged' ? t('cleanup.runMerged', { name: policy.name }) : t('cleanup.runQueued', { name: policy.name }))
    await list.reload()
  } catch (error) {
    notify.error(error)
  }
}

async function toggle(policy: CleanupPolicy, enabled: boolean): Promise<void> {
  try {
    await call(api.PUT('/cleanup-policies/{policy_id}', { params: { path: { policy_id: policy.id } }, body: toWrite(policy, { enabled }) }))
    notify.success(enabled ? t('cleanup.enabledToast', { name: policy.name }) : t('cleanup.pausedToast', { name: policy.name }))
    await list.reload()
  } catch (error) {
    notify.error(error)
  }
}

function confirmDelete(policy: CleanupPolicy): void {
  $q.dialog({
    title: t('cleanup.deleteTitle', { name: policy.name }),
    message: t('cleanup.deleteText', { count: policy.repositories.length }),
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'negative', label: t('cleanup.action.delete') },
    persistent: true,
  }).onOk(async () => {
    try {
      await call(api.DELETE('/cleanup-policies/{policy_id}', { params: { path: { policy_id: policy.id } } }))
      notify.success(t('cleanup.deleted', { name: policy.name }))
      await list.reload()
    } catch (error) {
      notify.error(error)
    }
  })
}
</script>

<style scoped>
.cleanup {
  display: grid;
  gap: 16px;
}
</style>
