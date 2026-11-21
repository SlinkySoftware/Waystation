<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Repository "Activity" (UI_DESIGN §4.3, T-U04): the Activity page's live log narrowed to this repository
     (`GET /activity?repo=` plus the live tail), with pause, the detail drawer and a link to the full log with every
     filter. -->
<template>
  <q-card class="repo-act">
    <div class="repo-act__bar">
      <span class="repo-act__state" role="status">
        <span class="repo-act__dot" :class="{ 'repo-act__dot--live': log.following.value }" aria-hidden="true" />
        {{ log.paused.value ? t('activity.paused') : log.held.value ? t('activity.holding') : t('activity.following') }}
      </span>
      <q-space />
      <q-btn outline no-caps dense class="q-px-sm" :icon="log.paused.value ? iconPlay : iconPause" :label="log.paused.value ? t('activity.resume') : t('activity.pause')" @click="togglePause" />
      <q-btn flat no-caps dense class="q-px-sm" :icon="iconExternal" :label="t('ui.repoActivity.openLog')" :to="{ name: 'activity', query: { repo: repo.name } }" />
    </div>
    <div class="repo-act__body">
      <button v-if="newCount" type="button" class="repo-act__new" @click="showNew">
        <q-icon :name="iconArrowUp" size="16px" />{{ t('activity.newEvents', { count: newCount }) }}
      </button>
      <div v-if="log.error.value && !log.items.value.length" class="q-pa-lg">
        <LoadFailedState :error="log.error.value" :title="t('activity.loadFailed')" @retry="log.reload" />
      </div>
      <div v-else-if="!log.items.value.length && log.loading.value" class="q-pa-md">
        <q-skeleton v-for="i in 6" :key="i" type="text" height="36px" class="q-mb-xs" />
      </div>
      <EmptyState v-else-if="!log.items.value.length" compact illustration="activity" :title="t('activity.emptyTitle')" :text="t('ui.repoActivity.emptyText')" />
      <ActivityList
        v-else
        ref="list"
        :items="log.items.value"
        :loading="log.loading.value"
        :done="log.done.value"
        :selected-id="selected?.id ?? null"
        :layout="$q.screen.width < 900 ? 'compact' : 'medium'"
        @select="(e) => (selected = e)"
        @more="log.loadMore"
        @at-top="log.setAtTop"
      />
    </div>
    <ActivityDetail :event="selected" :has-previous="index > 0" :has-next="index >= 0 && index < log.items.value.length - 1" @close="selected = null" @step="step" @narrow="narrow" />
  </q-card>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'

import type { ActivityEvent, Repository } from '@/api/types'
import ActivityDetail from '@/components/activity/ActivityDetail.vue'
import ActivityList from '@/components/activity/ActivityList.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadFailedState from '@/components/LoadFailedState.vue'
import { useActivityLog } from '@/composables/useActivityLog'
import { t } from '@/i18n'
import { type ActivityFilters, filtersToQuery, NO_FILTERS } from '@/utils/activity'
import { iconArrowUp, iconExternal, iconPause, iconPlay } from '@/utils/icons'

const props = defineProps<{ repo: Repository }>()
const $q = useQuasar()
const router = useRouter()

const filters = computed<ActivityFilters>(() => ({ ...NO_FILTERS, repo: props.repo.name }))
const log = useActivityLog(filters)
const list = ref<InstanceType<typeof ActivityList> | null>(null)
const selected = shallowRef<ActivityEvent | null>(null)
const index = computed(() => (selected.value ? log.items.value.findIndex((e) => e.id === selected.value!.id) : -1))
const newCount = computed(() => log.pending.value.length + log.overflow.value)

function togglePause(): void {
  if (log.paused.value) showNew()
  else log.pause()
}

function showNew(): void {
  log.resume()
  list.value?.scrollToTop()
}

function step(delta: number): void {
  const next = log.items.value[index.value + delta]
  if (next) selected.value = next
}

/** Narrowing further than the repository happens on the Activity page, which has every filter. */
function narrow(change: Partial<ActivityFilters>): void {
  void router.push({ name: 'activity', query: filtersToQuery({ ...filters.value, ...change }) })
}
</script>

<style scoped>
.repo-act {
  overflow: hidden;
}

.repo-act__bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
}

.repo-act__state {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.repo-act__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--status-offline);
}

.repo-act__dot--live {
  background: var(--status-ok);
}

.repo-act__body {
  position: relative;
}

.repo-act__new {
  position: absolute;
  top: 44px;
  left: 50%;
  z-index: 2;
  transform: translateX(-50%);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border: 0;
  border-radius: 999px;
  background: var(--q-primary);
  color: var(--text-on-brand);
  font: inherit;
  font-size: var(--fs-13);
  font-weight: 600;
  cursor: pointer;
  box-shadow: var(--shadow-pop);
}
</style>
