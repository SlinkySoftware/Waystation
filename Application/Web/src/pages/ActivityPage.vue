<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Activity (UI_DESIGN §4.6): the live, filterable event log. Follows the tail until paused or scrolled; pages back into
     history; filters live in the URL; a row opens its details; the current filter exports as CSV. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.activity')" :caption="t('activity.caption')">
      <template #actions>
        <span class="act-state" :class="`act-state--${state.key}`" role="status">
          <span class="act-state__dot" aria-hidden="true" />
          {{ state.label }}
          <q-tooltip>{{ state.help }}</q-tooltip>
        </span>
        <q-btn
          v-if="log.followable.value"
          outline
          no-caps
          :icon="log.paused.value ? iconPlay : iconPause"
          :label="$q.screen.lt.sm ? undefined : log.paused.value ? t('activity.resume') : t('activity.pause')"
          :aria-label="log.paused.value ? t('activity.resume') : t('activity.pause')"
          @click="togglePause"
        />
        <q-btn unelevated no-caps color="primary" :icon="iconDownload" :label="$q.screen.lt.sm ? undefined : t('activity.export')" :aria-label="t('activity.export')" :loading="exporting" @click="exportCsv">
          <q-tooltip>{{ t('activity.exportHelp') }}</q-tooltip>
        </q-btn>
      </template>
    </PageHeader>

    <q-card class="act-card">
      <ActivityToolbar :model-value="filters" :repositories="repoNames" @update:model-value="setFilters" />

      <div class="act-body">
      <transition name="act-banner">
        <button v-if="newCount" type="button" class="act-banner" @click="showNew">
          <q-icon :name="iconArrowUp" size="16px" />
          {{ newLabel }}
        </button>
      </transition>

      <div v-if="log.error.value && !log.items.value.length" class="q-pa-lg">
        <EmptyState :icon="iconError" :title="t('activity.loadFailed')" :text="errorMessage(log.error.value)">
          <q-btn unelevated no-caps color="primary" :label="t('common.retry')" @click="log.reload" />
        </EmptyState>
      </div>
      <div v-else-if="!log.items.value.length && log.loading.value" class="q-pa-md">
        <q-skeleton v-for="i in 8" :key="i" type="text" height="36px" class="q-mb-xs" />
      </div>
      <div v-else-if="!log.items.value.length" class="q-pa-lg">
        <EmptyState
          :icon="iconActivity"
          :title="filtered ? t('activity.noMatchTitle') : t('activity.emptyTitle')"
          :text="filtered ? t('activity.noMatchText') : t('activity.emptyText')"
        >
          <q-btn v-if="filtered" flat no-caps :label="t('activity.clear')" @click="setFilters({ ...NO_FILTERS })" />
        </EmptyState>
      </div>
      <ActivityList
        v-else
        ref="list"
        :items="log.items.value"
        :loading="log.loading.value"
        :done="log.done.value"
        :selected-id="selected?.id ?? null"
        :layout="layout"
        @select="(e) => (selected = e)"
        @more="log.loadMore"
        @at-top="log.setAtTop"
      />
      </div>

      <footer class="act-card__foot">
        <span>{{ t('activity.showing', { count: log.items.value.length }) }}</span>
        <span v-if="log.error.value && log.items.value.length" class="text-negative">
          {{ errorMessage(log.error.value) }}
          <q-btn flat dense no-caps :label="t('common.retry')" @click="retryMore" />
        </span>
      </footer>
    </q-card>

    <ActivityDetail :event="selected" :has-previous="selectedIndex > 0" :has-next="selectedIndex >= 0 && selectedIndex < log.items.value.length - 1" @close="selected = null" @step="step" @narrow="narrow" />
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, onMounted, ref, shallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, call, errorMessage } from '@/api/client'
import type { ActivityEvent } from '@/api/types'
import ActivityDetail from '@/components/activity/ActivityDetail.vue'
import ActivityList from '@/components/activity/ActivityList.vue'
import ActivityToolbar from '@/components/activity/ActivityToolbar.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useActivityLog } from '@/composables/useActivityLog'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'
import { type ActivityFilters, activeFilterCount, filenameFrom, filtersFromQuery, filtersToQuery, NO_FILTERS, toApiQuery } from '@/utils/activity'
import { iconActivity, iconArrowUp, iconDownload, iconError, iconPause, iconPlay } from '@/utils/icons'

const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const notify = useNotify()
const repos = useRepositoriesStore()

const filters = computed(() => filtersFromQuery(route.query))
const filtered = computed(() => activeFilterCount(filters.value) > 0)
function setFilters(next: ActivityFilters): void {
  void router.replace({ query: filtersToQuery(next) })
}
function narrow(change: Partial<ActivityFilters>): void {
  selected.value = null
  setFilters({ ...filters.value, ...change })
}

const log = useActivityLog(filters)
const list = ref<InstanceType<typeof ActivityList> | null>(null)
const selected = shallowRef<ActivityEvent | null>(null)

onMounted(() => void repos.load().catch(() => undefined))
const repoNames = computed(() => repos.items.map((r) => r.name))

const layout = computed(() => ($q.screen.width < 900 ? 'compact' : $q.screen.width < 1360 ? 'medium' : 'full'))

const newCount = computed(() => log.pending.value.length + log.overflow.value)
const newLabel = computed(() => (log.overflow.value ? t('activity.newEventsMore', { count: newCount.value }) : t('activity.newEvents', { count: newCount.value })))

const state = computed(() => {
  if (!log.followable.value) return { key: 'history', label: t('activity.historical'), help: t('activity.historicalHelp') }
  if (log.paused.value) return { key: 'paused', label: t('activity.paused'), help: t('activity.pausedHelp') }
  if (log.liveStatus.value !== 'open') return { key: 'reconnecting', label: t('activity.reconnecting'), help: t('activity.followingHelp') }
  if (log.held.value) return { key: 'holding', label: t('activity.holding'), help: t('activity.holdingHelp') }
  return { key: 'following', label: t('activity.following'), help: t('activity.followingHelp') }
})

function togglePause(): void {
  if (log.paused.value) showNew()
  else log.pause()
}

function showNew(): void {
  log.resume()
  list.value?.scrollToTop()
}

function retryMore(): void {
  log.error.value = null
  void log.loadMore()
}

const selectedIndex = computed(() => (selected.value ? log.items.value.findIndex((e) => e.id === selected.value!.id) : -1))
function step(delta: number): void {
  const next = log.items.value[selectedIndex.value + delta]
  if (next) selected.value = next
}

const exporting = ref(false)
async function exportCsv(): Promise<void> {
  exporting.value = true
  try {
    let disposition: string | null = null
    const blob = await call(
      api.GET('/activity/export', { params: { query: toApiQuery(filters.value) }, parseAs: 'blob' }).then((result) => {
        disposition = result.response.headers.get('Content-Disposition')
        return result
      }),
    )
    const url = URL.createObjectURL(blob as Blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filenameFrom(disposition, 'waystation-activity.csv')
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 10_000)
    notify.success(t('activity.exported'))
  } catch (error) {
    notify.error(error, t('activity.exportFailed'))
  } finally {
    exporting.value = false
  }
}
</script>

<style scoped>
.act-card {
  overflow: hidden;
}

.act-body {
  position: relative;
}

.act-card__foot {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 16px;
  border-top: 1px solid var(--border);
  font-size: var(--fs-12);
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.act-banner {
  position: absolute;
  z-index: 2;
  left: 50%;
  top: 44px;
  transform: translateX(-50%);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 14px;
  border: 0;
  border-radius: 16px;
  background: var(--q-primary);
  color: var(--text-on-brand);
  font: inherit;
  font-size: var(--fs-13);
  font-weight: 600;
  box-shadow: var(--shadow-pop);
  cursor: pointer;
}

.act-banner:focus-visible {
  outline: 2px solid var(--text-primary);
  outline-offset: 2px;
}

.act-banner-enter-active,
.act-banner-leave-active {
  transition: opacity var(--dur) var(--ease-out), transform var(--dur) var(--ease-out);
}

.act-banner-enter-from,
.act-banner-leave-to {
  opacity: 0;
  transform: translate(-50%, -6px);
}

.act-state {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 36px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: 18px;
  font-size: var(--fs-13);
  font-weight: 550;
  color: var(--text-secondary);
}

.act-state__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--status-offline);
}

.act-state--following .act-state__dot {
  background: var(--status-ok);
  animation: act-pulse 1.6s ease-out infinite;
}

.act-state--paused .act-state__dot,
.act-state--holding .act-state__dot,
.act-state--reconnecting .act-state__dot {
  background: var(--status-degraded);
}

@keyframes act-pulse {
  0% {
    box-shadow: 0 0 0 0 var(--status-ok-bg);
  }
  70% {
    box-shadow: 0 0 0 7px transparent;
  }
  100% {
    box-shadow: 0 0 0 0 transparent;
  }
}

@media (prefers-reduced-motion: reduce) {
  .act-state--following .act-state__dot {
    animation: none;
  }
}
</style>
