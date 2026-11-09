<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Repository Cleanup tab (UI_DESIGN §4.3): attached policies in run order with their last runs, attach / detach /
     reorder (saved at once with PUT, needs `manage` on the repository), and what they would remove here now. -->
<template>
  <div class="repo-cleanup">
    <PageCard :title="t('cleanup.repo.title')" :caption="t('cleanup.repo.caption', { name: repo.name })" flush>
      <template #actions>
        <q-btn v-if="isAdmin" flat no-caps color="primary" :icon="iconCleanup" :label="t('cleanup.repo.manage')" :to="{ name: 'cleanup' }" />
      </template>

      <EmptyState v-if="state.error.value && !state.data.value" compact :illustration="forbidden ? 'notFound' : 'error'" :title="forbidden ? t('cleanup.repo.forbidden') : errorMessage(state.error.value)">
        <q-btn v-if="!forbidden" flat no-caps :label="t('common.retry')" @click="state.reload" />
      </EmptyState>
      <q-skeleton v-else-if="!state.data.value" height="120px" class="q-ma-md" />
      <template v-else>
        <ol v-if="attached.length" class="repo-cleanup__list">
          <li v-for="(policy, i) in attached" :key="policy.id" class="repo-cleanup__item">
            <span class="repo-cleanup__order num">{{ i + 1 }}</span>
            <div class="repo-cleanup__main">
              <div class="repo-cleanup__name">
                <router-link v-if="isAdmin" :to="{ name: 'cleanup', query: { edit: policy.id } }" class="text-link">{{ policy.name }}</router-link>
                <span v-else>{{ policy.name }}</span>
                <PolicyFormat :format="policy.format" />
                <span v-if="!policy.enabled" class="ws-badge tone-offline">{{ t('cleanup.paused') }}</span>
              </div>
              <CriteriaChips :criteria="policy.criteria" />
              <div class="repo-cleanup__schedule">{{ describeSchedule(policy.schedule) }}</div>
            </div>
            <LastRun :run="policy.last_run" class="repo-cleanup__run" />
            <div v-if="canManage" class="repo-cleanup__actions">
              <q-btn flat round dense :icon="iconUp" :disable="saving || i === 0" :aria-label="t('cleanup.repo.moveUp', { name: policy.name })" @click="move(i, -1)" />
              <q-btn flat round dense :icon="iconDown" :disable="saving || i === attached.length - 1" :aria-label="t('cleanup.repo.moveDown', { name: policy.name })" @click="move(i, 1)" />
              <q-btn flat round dense color="negative" :icon="iconDetach" :disable="saving" :aria-label="t('cleanup.repo.detachWhat', { name: policy.name })" @click="detach(policy)">
                <q-tooltip>{{ t('cleanup.repo.detach') }}</q-tooltip>
              </q-btn>
            </div>
          </li>
        </ol>
        <EmptyState v-else compact illustration="cache" :title="t('cleanup.repo.empty')" :text="t('cleanup.repo.emptyText', { format: formatLabel(repo.format) })" />

        <div v-if="canManage" class="repo-cleanup__attach">
          <q-select
            v-model="choice"
            outlined
            dense
            emit-value
            map-options
            options-dense
            :options="options"
            :label="t('cleanup.repo.attachLabel')"
            :hint="options.length ? t('cleanup.repo.attachHint', { format: formatLabel(repo.format) }) : t('cleanup.repo.noneCompatible')"
            :disable="!options.length || saving"
            class="repo-cleanup__select"
          />
          <q-btn unelevated no-caps color="primary" :icon="iconAdd" :label="t('cleanup.repo.attach')" :disable="!choice" :loading="saving" @click="attach" />
        </div>
        <div v-else class="repo-cleanup__readonly">{{ t('cleanup.repo.readOnly') }}</div>
      </template>
    </PageCard>

    <RepoCleanupImpact v-if="attached.length" :repo="repo" :policies="attached" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { CleanupPolicy, Repository } from '@/api/types'
import { compatible, describeSchedule, formatScope } from '@/components/cleanup/cleanup'
import CriteriaChips from '@/components/cleanup/CriteriaChips.vue'
import LastRun from '@/components/cleanup/LastRun.vue'
import PolicyFormat from '@/components/cleanup/PolicyFormat.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { usePolling } from '@/composables/usePolling'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { formatLabel } from '@/utils/domain'
import { iconAdd, iconCleanup, iconDetach, iconDown, iconUp } from '@/utils/icons'

import RepoCleanupImpact from './RepoCleanupImpact.vue'

const props = defineProps<{ repo: Repository }>()
const auth = useAuthStore()
const notify = useNotify()
const isAdmin = computed(() => auth.can('admin'))
const canManage = computed(() => auth.can('repositories.manage'))

const state = useAsync(() => call(api.GET('/repositories/{name}/cleanup', { params: { path: { name: props.repo.name } } })), [() => props.repo.name])
const all = useAsync(() => call(api.GET('/cleanup-policies')))
const attached = computed(() => state.data.value?.policies ?? [])
const forbidden = computed(() => state.error.value instanceof ApiError && [403, 404].includes(state.error.value.status))
usePolling(() => attached.value.some((p) => p.last_run?.status === 'running'), () => state.reload())

const choice = ref<string | null>(null)
const saving = ref(false)
const options = computed(() =>
  (all.data.value?.items ?? [])
    .filter((p) => compatible(p, props.repo.format) && !attached.value.some((a) => a.id === p.id))
    .map((p) => ({ value: p.id, label: `${p.name} · ${formatScope(p.format)}` })),
)

async function put(ids: string[], message: string): Promise<void> {
  saving.value = true
  try {
    state.data.value = await call(api.PUT('/repositories/{name}/cleanup', { params: { path: { name: props.repo.name } }, body: { policy_ids: ids } }))
    notify.success(message)
  } catch (error) {
    notify.error(error)
  } finally {
    saving.value = false
  }
}

async function attach(): Promise<void> {
  const policy = all.data.value?.items.find((p) => p.id === choice.value)
  if (!policy) return
  await put([...attached.value.map((p) => p.id), policy.id], t('cleanup.repo.attached', { name: policy.name }))
  choice.value = null
}

function detach(policy: CleanupPolicy): void {
  void put(attached.value.filter((p) => p.id !== policy.id).map((p) => p.id), t('cleanup.repo.detached', { name: policy.name }))
}

function move(index: number, delta: number): void {
  const ids = attached.value.map((p) => p.id)
  const [id] = ids.splice(index, 1)
  ids.splice(index + delta, 0, id!)
  void put(ids, t('cleanup.repo.reordered'))
}
</script>

<style scoped>
.repo-cleanup {
  display: grid;
  gap: 12px;
}

.repo-cleanup__list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.repo-cleanup__item {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) minmax(160px, 240px) auto;
  gap: 12px;
  align-items: center;
  padding: 12px 16px;
  border-top: 1px solid var(--border);
}

.repo-cleanup__order {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--surface-brand);
  color: var(--q-primary);
  font-size: var(--fs-12);
  font-weight: 650;
}

.repo-cleanup__main {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.repo-cleanup__name {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  font-weight: 600;
}

.repo-cleanup__name :deep(.policy-format) {
  font-weight: 400;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.repo-cleanup__schedule,
.repo-cleanup__readonly {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.repo-cleanup__actions {
  display: flex;
  gap: 2px;
}

.repo-cleanup__attach {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 8px 12px;
  padding: 12px 16px 16px;
  border-top: 1px solid var(--border);
}

.repo-cleanup__select {
  flex: 1 1 280px;
  max-width: 460px;
}

.repo-cleanup__readonly {
  padding: 12px 16px;
  border-top: 1px solid var(--border);
}

@media (max-width: 799px) {
  .repo-cleanup__item {
    grid-template-columns: 28px minmax(0, 1fr);
  }

  .repo-cleanup__run,
  .repo-cleanup__actions {
    grid-column: 2;
  }
}
</style>
