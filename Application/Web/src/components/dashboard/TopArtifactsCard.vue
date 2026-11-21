<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Top artifacts (most downloaded / most bytes served over a window) or recently added artifacts (UI_DESIGN §4.2
     item 6), from /stats/top/artifacts. Rows link to the artifact in the repository's Browse tab. -->
<template>
  <PageCard :title="title" :caption="caption" flush>
    <template v-if="mode === 'top'" #actions>
      <q-btn-toggle v-model="measure" no-caps dense unelevated toggle-color="primary" class="seg" :options="measureOptions" :aria-label="t('dashboard.top.title')" />
      <q-btn-toggle v-model="topWindow" no-caps dense unelevated toggle-color="primary" class="seg" :options="windowOptions" :aria-label="t('dashboard.top.window')" />
    </template>
    <div v-if="error && !data" class="q-pa-md text-negative">{{ t('dashboard.top.loadFailed') }}</div>
    <div v-else-if="!data" class="q-pa-md"><q-skeleton v-for="i in 5" :key="i" height="28px" class="q-mb-sm" /></div>
    <EmptyState
      v-else-if="!data.items.length"
      compact
      :icon="mode === 'top' ? iconDownload : iconPackage"
      :title="mode === 'top' ? t('dashboard.top.empty') : t('dashboard.recent.empty')"
      :text="mode === 'top' ? t('dashboard.top.emptyText') : t('dashboard.recent.emptyText')"
    />
    <ol v-else class="top" :aria-label="mode === 'top' ? t('dashboard.top.aria') : t('dashboard.recent.aria')">
      <li v-for="(a, i) in data.items" :key="a.component_id" class="top__row">
        <span v-if="mode === 'top'" class="top__rank num">{{ i + 1 }}</span>
        <FormatIcon :mark="repos.markFor(a.repository, a.format)" :size="18" decorative />
        <div class="top__body">
          <router-link :to="artifactLocation(a)" class="top__name mono" :title="a.display">{{ truncateMiddle(a.display, 56) }}</router-link>
          <div class="top__meta">
            <RepoChip :name="a.repository" :format="a.format" :mark-override="repos.markFor(a.repository, a.format)" />
            <span>{{ formatBytes(a.size_bytes) }}</span>
            <TimeCell v-if="mode === 'recent'" :value="a.created_at" />
            <span v-else-if="a.last_downloaded_at" class="text-faint"><TimeCell :value="a.last_downloaded_at" /></span>
          </div>
        </div>
        <div v-if="mode === 'top'" class="top__value num">
          <template v-if="measure === 'downloads'">{{ formatCount(a.downloads) }}</template>
          <template v-else>{{ formatBytes(a.bytes_served) }}</template>
          <div v-if="maxValue > 0" class="top__bar" aria-hidden="true"><div :style="{ width: `${(100 * value(a)) / maxValue}%`, background: formatColor(a.format) }" /></div>
        </div>
      </li>
    </ol>
  </PageCard>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { api, call } from '@/api/client'
import type { TopArtifact, TopWindow } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import PageCard from '@/components/PageCard.vue'
import RepoChip from '@/components/RepoChip.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatColor } from '@/utils/domain'
import { formatBytes, formatCount, truncateMiddle } from '@/utils/format'
import { iconDownload, iconPackage } from '@/utils/icons'
import { readString, writeString } from '@/utils/storage'
import { artifactLocation } from '@/utils/topStats'

const props = withDefaults(defineProps<{ mode?: 'top' | 'recent'; limit?: number; repository?: string | null }>(), { mode: 'top', limit: 8, repository: null })

const WINDOWS: TopWindow[] = ['24h', '7d', '30d', 'all']
const repos = useRepositoriesStore()
const storedMeasure = readString('ws.dashboard.top.measure')
const storedWindow = readString('ws.dashboard.top.window') as TopWindow | null
const measure = ref<'downloads' | 'bytes'>(storedMeasure === 'bytes' ? 'bytes' : 'downloads')
const topWindow = ref<TopWindow>(storedWindow && WINDOWS.includes(storedWindow) ? storedWindow : '7d')

const measureOptions = computed(() => [
  { label: t('dashboard.top.downloads'), value: 'downloads' },
  { label: t('dashboard.top.bytes'), value: 'bytes' },
])
const windowOptions = computed(() => WINDOWS.map((w) => ({ label: t(`dashboard.top.windows.${w}`), value: w })))

const { data, error } = useAsync(() => {
  if (props.mode === 'top') {
    writeString('ws.dashboard.top.measure', measure.value)
    writeString('ws.dashboard.top.window', topWindow.value)
  }
  const by = props.mode === 'recent' ? 'recent' : measure.value
  return call(api.GET('/stats/top/artifacts', { params: { query: { by, window: topWindow.value, limit: props.limit, repo: props.repository ?? undefined } } }))
}, [measure, topWindow])

const title = computed(() => (props.mode === 'top' ? t('dashboard.top.title') : t('dashboard.recent.title')))
const caption = computed(() => {
  if (props.mode === 'recent') return t('dashboard.recent.caption')
  const w = t(`dashboard.top.windowLong.${topWindow.value}`)
  return measure.value === 'downloads' ? t('dashboard.top.captionDownloads', { window: w }) : t('dashboard.top.captionBytes', { window: w })
})

const value = (a: TopArtifact) => (measure.value === 'downloads' ? a.downloads : a.bytes_served)
const maxValue = computed(() => Math.max(0, ...(data.value?.items ?? []).map(value)))
</script>

<style scoped>
.seg {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.seg :deep(.q-btn) {
  padding: 0 10px;
  min-height: 28px;
}

.top {
  list-style: none;
  margin: 0;
  padding: 0;
}

.top__row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  border-top: 1px solid var(--border);
}

.top__rank {
  flex: none;
  width: 18px;
  text-align: right;
  color: var(--text-secondary);
  font-size: var(--fs-12);
}

.top__body {
  flex: 1;
  min-width: 0;
}

.top__name {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-13);
  color: var(--text-primary);
  text-decoration: none;
}

.top__name:hover {
  color: var(--q-primary);
  text-decoration: underline;
}

.top__meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin-top: 3px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.top__meta :deep(.repo-chip) {
  height: 19px;
  font-size: 11px;
}

.top__value {
  flex: none;
  width: 92px;
  text-align: right;
  font-size: var(--fs-13);
  font-weight: 600;
}

.top__bar {
  height: 3px;
  margin-top: 4px;
  border-radius: 2px;
  background: var(--surface-subtle);
  overflow: hidden;
}

.top__bar > div {
  height: 100%;
  border-radius: 2px;
}
</style>
