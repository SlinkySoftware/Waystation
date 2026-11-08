<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Recent activity: the durable events (misses, failures, cleanup, repository changes), newest first, live. -->
<template>
  <PageCard :title="t('dashboard.activity.title')" flush>
    <template #actions>
      <q-btn flat dense no-caps :label="t('dashboard.activity.all')" :to="{ name: 'activity' }" />
    </template>
    <q-skeleton v-if="!items.length && loading" height="200px" class="q-ma-md" />
    <EmptyState v-else-if="!items.length" compact :title="t('dashboard.activity.empty')" />
    <TransitionGroup v-else tag="ul" name="feed" class="feed">
      <li v-for="e in items" :key="e.id" class="feed__item">
        <span class="feed__icon" :class="tone(e)"><q-icon :name="icon(e)" size="16px" /></span>
        <div class="feed__body">
          <div class="feed__summary">{{ e.summary }}</div>
          <div class="feed__meta">
            <RepoChip v-if="e.repository" :name="e.repository" :format="e.format ?? null" :mark-override="e.format ? repos.markFor(e.repository, e.format) : undefined" />
            <span v-if="e.principal">{{ e.principal }}</span>
            <span v-if="e.bytes">{{ formatBytes(e.bytes) }}</span>
            <TimeCell :value="e.ts" />
          </div>
        </div>
      </li>
    </TransitionGroup>
  </PageCard>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { api, call } from '@/api/client'
import type { ActivityEvent } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import RepoChip from '@/components/RepoChip.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { useLiveStore } from '@/stores/live'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatBytes } from '@/utils/format'
import { iconCleanup, iconError, iconFetching, iconHit, iconInfo, iconLock, iconRepositories, iconWarning } from '@/utils/icons'

const props = withDefaults(defineProps<{ limit?: number; repository?: string | null }>(), { limit: 8, repository: null })

const live = useLiveStore()
const repos = useRepositoriesStore()
const { data, loading } = useAsync(() => call(api.GET('/activity', { params: { query: { limit: 30, repo: props.repository ?? undefined } } })))

const items = computed(() => {
  const seen = new Set<string>()
  const merged: ActivityEvent[] = []
  for (const e of [...live.activity, ...(data.value?.items ?? [])]) {
    if (seen.has(e.id) || (props.repository && e.repository !== props.repository)) continue
    seen.add(e.id)
    merged.push(e)
  }
  return merged.sort((a, b) => (a.ts < b.ts ? 1 : -1)).slice(0, props.limit)
})

function icon(e: ActivityEvent): string {
  switch (e.kind) {
    case 'cache_miss':
      return iconFetching
    case 'download':
      return iconHit
    case 'error':
      return iconError
    case 'upstream_state':
      return iconWarning
    case 'cleanup':
      return iconCleanup
    case 'auth':
      return iconLock
    case 'repository':
      return iconRepositories
    default:
      return iconInfo
  }
}

function tone(e: ActivityEvent): string {
  if (e.outcome === 'failure') return 'tone-error'
  if (e.kind === 'cache_miss') return 'tone-miss'
  if (e.kind === 'download') return 'tone-hit'
  return 'tone-offline'
}
</script>

<style scoped>
.feed {
  list-style: none;
  margin: 0;
  padding: 0;
}

.feed__item {
  display: flex;
  gap: 12px;
  padding: 10px 16px;
  border-top: 1px solid var(--border);
}

.feed__icon {
  display: grid;
  place-items: center;
  flex: none;
  width: 28px;
  height: 28px;
  border-radius: 8px;
}

.feed__body {
  min-width: 0;
  flex: 1;
}

.feed__summary {
  font-size: var(--fs-13);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.feed__meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin-top: 3px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.feed__meta :deep(.repo-chip) {
  height: 19px;
  font-size: 11px;
}

.feed-enter-active {
  transition: opacity 300ms var(--ease-out), transform 300ms var(--ease-out);
}

.feed-enter-from {
  opacity: 0;
  transform: translateY(-6px);
}

.feed-leave-active {
  display: none;
}

.feed-move {
  transition: transform 300ms var(--ease-out);
}
</style>
