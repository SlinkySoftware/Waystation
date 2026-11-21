<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Largest artifacts (UI_DESIGN §4.7): the components holding the most cached bytes, with a "Where used" lookup of
     their content digest. -->
<template>
  <PageCard :title="t('storage.artifacts.title')" :caption="t('storage.artifacts.caption', { count: limit })" flush>
    <div v-if="error && !data" class="q-pa-md text-negative">{{ t('storage.artifacts.loadFailed') }}</div>
    <div v-else-if="!data" class="q-pa-md"><q-skeleton v-for="i in 5" :key="i" height="28px" class="q-mb-sm" /></div>
    <EmptyState v-else-if="!data.items.length" compact :icon="iconStorage" :title="t('storage.artifacts.none')" />
    <ol v-else class="la">
      <li v-for="a in data.items" :key="a.component_id" class="la__row">
        <FormatIcon :mark="repos.markFor(a.repository, a.format)" :size="18" decorative />
        <div class="la__body">
          <router-link :to="artifactLocation(a)" class="la__name mono" :title="a.display">{{ truncateMiddle(a.display, 60) }}</router-link>
          <div class="la__meta">
            <RepoChip :name="a.repository" :format="a.format" :mark-override="repos.markFor(a.repository, a.format)" />
            <span>{{ t('dashboard.top.downloadCount', { count: a.downloads }) }}</span>
          </div>
        </div>
        <div class="la__size num">
          {{ formatBytes(a.size_bytes) }}
          <div class="la__bar" aria-hidden="true"><div :style="{ width: `${max ? (100 * a.size_bytes) / max : 0}%`, background: formatColor(a.format) }" /></div>
        </div>
        <q-btn v-if="a.sha256" flat dense round size="sm" :icon="iconLink" :aria-label="t('storage.artifacts.whereUsedFor', { name: a.display })" @click="pick(a)">
          <q-tooltip>{{ t('storage.artifacts.whereUsed') }}</q-tooltip>
        </q-btn>
      </li>
    </ol>
    <WhereUsedDialog v-if="chosen" v-model="dialog" :digest="chosen.sha256 ?? ''" :name="chosen.display" />
  </PageCard>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { api, call } from '@/api/client'
import type { TopArtifact } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import PageCard from '@/components/PageCard.vue'
import RepoChip from '@/components/RepoChip.vue'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatColor } from '@/utils/domain'
import { formatBytes, truncateMiddle } from '@/utils/format'
import { iconLink, iconStorage } from '@/utils/icons'
import { artifactLocation } from '@/utils/topStats'

import WhereUsedDialog from './WhereUsedDialog.vue'

const props = withDefaults(defineProps<{ limit?: number }>(), { limit: 10 })

const repos = useRepositoriesStore()
const { data, error } = useAsync(() => call(api.GET('/stats/top/artifacts', { params: { query: { by: 'size', limit: props.limit } } })))
const max = computed(() => Math.max(0, ...(data.value?.items ?? []).map((a) => a.size_bytes)))

const chosen = ref<TopArtifact | null>(null)
const dialog = ref(false)
function pick(a: TopArtifact): void {
  chosen.value = a
  dialog.value = true
}
</script>

<style scoped>
.la {
  list-style: none;
  margin: 0;
  padding: 0;
}

.la__row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px 8px 16px;
  border-top: 1px solid var(--border);
}

.la__body {
  flex: 1;
  min-width: 0;
}

.la__name {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-13);
  color: var(--text-primary);
  text-decoration: none;
}

.la__name:hover {
  color: var(--q-primary);
  text-decoration: underline;
}

.la__meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin-top: 3px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.la__meta :deep(.repo-chip) {
  height: 19px;
  font-size: 11px;
}

.la__size {
  flex: none;
  width: 92px;
  text-align: right;
  font-size: var(--fs-13);
  font-weight: 600;
}

.la__bar {
  height: 3px;
  margin-top: 4px;
  border-radius: 2px;
  background: var(--surface-subtle);
  overflow: hidden;
}

.la__bar > div {
  height: 100%;
  border-radius: 2px;
}
</style>
