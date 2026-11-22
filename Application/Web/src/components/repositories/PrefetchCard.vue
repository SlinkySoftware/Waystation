<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A proxy repository's prefetch at a glance (overview, users with `manage`): popular-metadata refresh, the warm list,
     its schedule and next run, the last warm run (with a link to it on the Tasks page for administrators), and
     "Warm now". Hidden when the format supports neither (or the server says the repository is not a proxy). -->
<template>
  <PageCard v-if="visible" :title="t('prefetch.card.title')" :caption="t('prefetch.card.caption')" data-test="prefetch-card">
    <template #actions>
      <q-btn flat no-caps :label="t('prefetch.card.edit')" :to="{ name: 'repository', params: { name: repo.name, tab: 'settings' } }" />
      <span>
        <q-btn
          unelevated
          no-caps
          color="primary"
          :icon="iconFetching"
          :label="t('prefetch.card.warmNow')"
          :loading="warming"
          :disable="!status || !status.warm_supported || status.warm_items === 0 || running"
          data-test="prefetch-warm-now"
          @click="warmNow"
        />
        <q-tooltip v-if="status && status.warm_items === 0">{{ t('prefetch.card.warmNowEmpty') }}</q-tooltip>
      </span>
    </template>

    <LoadFailedState v-if="state.error.value && !status" compact :error="state.error.value" :title="t('prefetch.card.loadFailed')" :loading="state.loading.value" @retry="state.reload" />
    <div v-else-if="!status" class="prefetch-card__grid" aria-busy="true">
      <q-skeleton v-for="i in 4" :key="i" type="rect" height="44px" />
    </div>
    <div v-else class="prefetch-card__grid">
      <div v-if="status.refresh_supported" class="prefetch-card__cell" data-test="prefetch-refresh-state">
        <div class="prefetch-card__label">{{ t('prefetch.card.refresh') }}</div>
        <div class="prefetch-card__value" :class="{ 'text-faint': !status.refresh_popular }">
          {{ status.refresh_popular ? t('prefetch.card.refreshOn') : t('prefetch.card.refreshOff') }}
        </div>
      </div>
      <div v-if="status.warm_supported" class="prefetch-card__cell" data-test="prefetch-items">
        <div class="prefetch-card__label">{{ t('prefetch.card.warmList') }}</div>
        <div class="prefetch-card__value" :class="{ 'text-faint': !status.warm_items }">
          {{ status.warm_items ? t('prefetch.card.items', { count: status.warm_items, n: formatCount(status.warm_items) }) : t('prefetch.card.noItems') }}
        </div>
      </div>
      <div v-if="status.warm_supported" class="prefetch-card__cell" data-test="prefetch-schedule-state">
        <div class="prefetch-card__label">{{ t('prefetch.card.schedule') }}</div>
        <div class="prefetch-card__value">{{ status.schedule ? describeTaskSchedule(status.schedule) : t('prefetch.card.manualOnly') }}</div>
        <div v-if="status.next_run_at" class="prefetch-card__sub">{{ t('prefetch.card.nextRun') }} <TimeCell :value="status.next_run_at" /></div>
      </div>
      <div v-if="status.warm_supported" class="prefetch-card__cell" data-test="prefetch-last-run">
        <div class="prefetch-card__label">{{ t('prefetch.card.lastRun') }}</div>
        <template v-if="last">
          <div class="prefetch-card__run">
            <RunStatusPill :status="last.status" />
            <TimeCell :value="last.started_at" class="prefetch-card__sub" />
            <router-link v-if="isAdmin" :to="{ name: 'tasks', query: { run: last.id } }" class="prefetch-card__link" data-test="prefetch-run-link">{{ t('prefetch.card.viewRun') }}</router-link>
          </div>
          <div class="prefetch-card__sub prefetch-card__figures">{{ figures }}</div>
          <div v-if="last.error" class="prefetch-card__error">{{ last.error }}<q-tooltip max-width="360px">{{ last.error }}</q-tooltip></div>
        </template>
        <div v-else class="prefetch-card__value text-faint">{{ t('prefetch.card.never') }}</div>
      </div>
    </div>
  </PageCard>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { api, ApiError, call } from '@/api/client'
import type { Repository } from '@/api/types'
import RunStatusPill from '@/components/cleanup/RunStatusPill.vue'
import LoadFailedState from '@/components/LoadFailedState.vue'
import PageCard from '@/components/PageCard.vue'
import { describeTaskSchedule } from '@/components/tasks/tasks'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { usePolling } from '@/composables/usePolling'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { formatCount } from '@/utils/format'
import { iconFetching } from '@/utils/icons'

const props = defineProps<{ repo: Repository }>()

const notify = useNotify()
const auth = useAuthStore()
// The Tasks page (and so the run drawer) is for administrators.
const isAdmin = computed(() => auth.can('admin'))

const name = computed(() => props.repo.name)
const state = useAsync(() => call(api.GET('/repositories/{name}/prefetch', { params: { path: { name: name.value } } })), [name, () => props.repo.updated_at])
const status = computed(() => state.data.value)
const last = computed(() => status.value?.last_run ?? null)
const running = computed(() => last.value?.status === 'running')
/** 409: not a proxy after all (the server decides); nothing to show. */
const notProxy = computed(() => state.error.value instanceof ApiError && state.error.value.status === 409)
const visible = computed(() => !notProxy.value && (!status.value || status.value.warm_supported || status.value.refresh_supported))

// A warm run has no live topic of its own here: poll while one is running.
usePolling(running, () => state.reload(), 3000)

const figures = computed(() => {
  const run = last.value
  if (!run) return ''
  const parts = [t('prefetch.card.runItems', { count: run.items, n: formatCount(run.items) })]
  if (run.failed) parts.push(t('prefetch.card.runFailed', { n: formatCount(run.failed) }))
  parts.push(t('prefetch.card.runFetched', { fetched: formatCount(run.fetched), files: formatCount(run.files) }))
  return parts.join(' · ')
})

const warming = ref(false)
async function warmNow(): Promise<void> {
  warming.value = true
  try {
    const job = await call(api.POST('/repositories/{name}/prefetch/run', { params: { path: { name: name.value } } }))
    notify.success(t(job.status === 'merged' || job.status === 'rerun' ? 'prefetch.card.merged' : 'prefetch.card.queued', { name: name.value }))
    await state.reload()
  } catch (error) {
    notify.error(error)
  } finally {
    warming.value = false
  }
}
</script>

<style scoped>
.prefetch-card__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px 20px;
  font-size: var(--fs-13);
}

.prefetch-card__cell {
  display: grid;
  align-content: start;
  gap: 3px;
  min-width: 0;
}

.prefetch-card__label {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.prefetch-card__value {
  font-weight: 600;
}

.prefetch-card__sub {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.prefetch-card__figures {
  font-variant-numeric: tabular-nums;
}

.prefetch-card__run {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 8px;
}

.prefetch-card__link {
  font-size: var(--fs-12);
  color: var(--q-primary);
  text-decoration: none;
}

.prefetch-card__link:hover,
.prefetch-card__link:focus-visible {
  text-decoration: underline;
}

.prefetch-card__error {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-12);
  color: var(--status-blocked);
}
</style>
