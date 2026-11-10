<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Browse: pick a repository; its Browse tab holds the format-aware view (UI_DESIGN §4.4). -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.browse')" :caption="t('browse.pageCaption')" />
    <div class="pick">
      <q-skeleton v-for="i in (repos.items.length || repos.loaded ? 0 : 6)" :key="i" height="96px" />
      <router-link v-for="r in repos.items" :key="r.name" :to="{ name: 'repository', params: { name: r.name, tab: 'browse' } }" class="pick__card">
        <FormatIcon :mark="repoMark(r)" :size="26" brand />
        <div class="pick__text">
          <div class="pick__name">{{ r.name }}</div>
          <div class="pick__meta">{{ formatLabel(r.format) }} · {{ formatBytes(r.stats.size_bytes) }} · {{ t('browse.artifactCount', { count: r.stats.artifact_count, n: formatCount(r.stats.artifact_count) }) }}</div>
        </div>
        <q-icon :name="iconChevronRight" size="20px" class="text-faint" />
      </router-link>
    </div>
    <q-card v-if="repos.loaded && !repos.items.length">
      <EmptyState illustration="repositories" :title="t('repos.emptyTitle')" :text="auth.can('repositories.create') ? t('repos.emptyText') : t('repos.emptyNoAccessText')" />
    </q-card>
  </q-page>
</template>

<script setup lang="ts">
import EmptyState from '@/components/EmptyState.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatLabel, repoMark } from '@/utils/domain'
import { formatBytes, formatCount } from '@/utils/format'
import { iconChevronRight } from '@/utils/icons'

const auth = useAuthStore()
const repos = useRepositoriesStore()
void repos.load(true).catch(() => undefined)
</script>

<style scoped>
.pick {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}

.pick__card {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow-card);
  color: var(--text-primary);
  text-decoration: none;
  transition: border-color var(--dur) var(--ease-out), transform var(--dur) var(--ease-out);
}

.pick__card:hover {
  border-color: var(--q-primary);
  transform: translateY(-1px);
}

.pick__text {
  flex: 1;
  min-width: 0;
}

.pick__name {
  font-weight: 650;
}

.pick__meta {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
