<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- The dashboard of a user without `stats.read` (SPEC §4.10 "What a user without roles sees"): the repositories the
     user can use, or, with no access at all, a friendly "no access yet" landing. It asks the server only for the
     repository list (filtered by RBAC), never for statistics or the event stream. -->
<template>
  <div class="landing">
    <PageCard v-if="canBrowse" :title="t('landing.reposTitle')" :caption="repos.loaded ? t('landing.reposCaption', { count: repos.items.length }) : t('common.loading')">
      <template #actions>
        <q-btn flat no-caps :icon="iconSearch" :label="t('landing.search')" :to="{ name: 'search' }" />
        <q-btn flat no-caps :icon="iconBrowse" :label="t('landing.browse')" :to="{ name: 'browse' }" />
      </template>
      <div v-if="!repos.loaded" class="landing__grid">
        <q-skeleton v-for="i in 4" :key="i" height="72px" />
      </div>
      <div v-else-if="repos.items.length" class="landing__grid">
        <router-link v-for="r in shown" :key="r.name" :to="{ name: 'repository', params: { name: r.name } }" class="landing__repo">
          <FormatIcon :mark="repoMark(r)" :size="24" brand />
          <div class="landing__text">
            <div class="landing__name">{{ r.name }}</div>
            <div class="landing__meta">{{ formatLabel(r.format) }} · {{ typeMeta(r.type).label }}</div>
          </div>
          <q-icon :name="iconChevronRight" size="20px" class="text-faint" />
        </router-link>
      </div>
      <EmptyState v-else compact illustration="repositories" :title="t('repos.emptyTitle')" :text="t('repos.emptyNoAccessText')" />
      <template #footer>
        <span class="landing__note">{{ t('landing.statsNote') }}</span>
      </template>
    </PageCard>
    <q-card v-else>
      <EmptyState illustration="repositories" :title="t('landing.noRolesTitle')" :text="t('landing.noRolesText')">
        <q-btn flat no-caps :label="t('landing.profile')" :to="{ name: 'profile' }" />
      </EmptyState>
    </q-card>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import EmptyState from '@/components/EmptyState.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import PageCard from '@/components/PageCard.vue'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatLabel, repoMark, typeMeta } from '@/utils/domain'
import { iconBrowse, iconChevronRight, iconSearch } from '@/utils/icons'

const MAX_SHOWN = 12

const auth = useAuthStore()
const repos = useRepositoriesStore()
const canBrowse = computed(() => auth.can('repositories.browse'))
const shown = computed(() => repos.items.slice(0, MAX_SHOWN))

if (canBrowse.value) void repos.load(true).catch(() => undefined)
</script>

<style scoped>
.landing__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 10px;
}

.landing__repo {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text-primary);
  text-decoration: none;
  transition: border-color var(--dur) var(--ease-out);
}

.landing__repo:hover {
  border-color: var(--q-primary);
}

.landing__text {
  flex: 1;
  min-width: 0;
}

.landing__name {
  font-weight: 650;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.landing__meta,
.landing__note {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
