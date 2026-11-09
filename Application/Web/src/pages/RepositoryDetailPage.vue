<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Repository detail: header (icon, name, type, status, URL, Set me up), KPI strip, tabs (UI_DESIGN §4.3). -->
<template>
  <q-page class="ws-page">
    <template v-if="repo">
      <PageHeader :crumbs="[{ label: t('nav.repositories'), to: { name: 'repositories' } }, { label: repo.name }]">
        <template #icon><FormatIcon :mark="repoMark(repo)" :size="26" brand /></template>
        <template #title>
          {{ repo.name }}
          <span class="detail__type"><q-icon :name="typeMeta(repo.type).icon" size="15px" />{{ typeMeta(repo.type).label }} · {{ formatLabel(repo.format) }}</span>
          <StatusPill :status="repo.status" />
        </template>
        <template #caption>
          <div class="detail__caption">
            <span v-if="repo.description">{{ repo.description }}</span>
            <CopyField :value="repo.url" :label="t('repos.url')" />
          </div>
        </template>
        <template #actions>
          <q-btn unelevated no-caps color="primary" :icon="iconSetup" :label="t('setup.button')" @click="setupOpen = true" />
        </template>
      </PageHeader>

      <div class="detail__kpis">
        <div v-for="k in kpis" :key="k.label" class="detail__kpi">
          <div class="detail__kpi-label">{{ k.label }}</div>
          <div class="detail__kpi-value">
            <component :is="k.component" v-if="k.component" v-bind="k.props" />
            <template v-else>{{ k.value }}</template>
          </div>
        </div>
      </div>

      <QuotaMeter v-if="repo.quota" :quota="repo.quota" class="detail__quota" />

      <q-tabs :model-value="tab" dense no-caps align="left" active-color="primary" indicator-color="primary" class="detail__tabs" narrow-indicator>
        <q-tab v-for="tb in tabs" :key="tb" :name="tb" :label="t(`detail.tab.${tb}`)" @click="router.push({ name: 'repository', params: { name: repo.name, tab: tb } })" />
      </q-tabs>

      <OverviewTab v-if="tab === 'overview'" :repo="repo" />
      <RepoBrowser v-else-if="tab === 'browse'" :repo="repo" />
      <CleanupTab v-else-if="tab === 'cleanup'" :repo="repo" />
      <SettingsTab v-else :repo="repo" @saved="onSaved" />

      <SetupPanel v-model="setupOpen" :repo="repo" />
    </template>

    <q-card v-else-if="state.error.value">
      <EmptyState v-if="missing" illustration="notFound" :title="t('detail.notFound')" :text="t('detail.notFoundText', { name })">
        <q-btn unelevated no-caps color="primary" :label="t('nav.repositories')" :to="{ name: 'repositories' }" />
      </EmptyState>
      <EmptyState v-else illustration="error" :title="t('detail.loadFailed')" :text="errorMessage(state.error.value)">
        <q-btn unelevated no-caps color="primary" :label="t('detail.retry')" :loading="state.loading.value" @click="state.reload()" />
        <q-btn flat no-caps :label="t('nav.repositories')" :to="{ name: 'repositories' }" />
      </EmptyState>
    </q-card>
    <template v-else>
      <q-skeleton type="text" width="320px" height="48px" />
      <q-skeleton height="88px" class="q-mt-md" />
      <q-skeleton height="320px" class="q-mt-md" />
    </template>
  </q-page>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { Repository } from '@/api/types'
import RepoBrowser from '@/components/browse/RepoBrowser.vue'
import CopyField from '@/components/CopyField.vue'
import EmptyState from '@/components/EmptyState.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import CleanupTab from '@/components/repositories/CleanupTab.vue'
import OverviewTab from '@/components/repositories/OverviewTab.vue'
import QuotaMeter from '@/components/repositories/QuotaMeter.vue'
import SettingsTab from '@/components/repositories/SettingsTab.vue'
import SetupPanel from '@/components/repositories/SetupPanel.vue'
import SizeCell from '@/components/SizeCell.vue'
import StatusPill from '@/components/StatusPill.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useLiveTopic } from '@/composables/useLiveTopic'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatLabel, repoMark, typeMeta } from '@/utils/domain'
import { formatBytes, formatCount, formatPercent } from '@/utils/format'
import { iconSetup } from '@/utils/icons'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
// Cleanup needs `manage` on the repository (a hint: the server decides, and the tab handles 403/404 itself).
const tabs = computed(() => (auth.can('repositories.manage') ? ['overview', 'browse', 'cleanup', 'settings'] : ['overview', 'browse', 'settings']) as readonly ('overview' | 'browse' | 'cleanup' | 'settings')[])
const repos = useRepositoriesStore()
const name = computed(() => String(route.params.name))
const tab = computed(() => (typeof route.params.tab === 'string' && route.params.tab ? route.params.tab : 'overview'))
const setupOpen = ref(route.query.setup === '1')

const state = useAsync(() => call(api.GET('/repositories/{name}', { params: { path: { name: name.value } } })), [name])
const override = ref<Repository | null>(null)
// Missing and hidden repositories are both 404; anything else is a load failure worth retrying.
const missing = computed(() => state.error.value instanceof ApiError && state.error.value.status === 404)
const repo = computed(() => override.value ?? state.data.value)

useLiveTopic(() => [`repo:${name.value}`])
void repos.load().catch(() => undefined)

const kpis = computed(() => {
  const s = repo.value?.stats
  if (!s) return []
  return [
    { label: t('detail.kpi.size'), component: SizeCell, props: { bytes: s.size_bytes } },
    { label: t('detail.kpi.artifacts'), value: formatCount(s.artifact_count) },
    { label: t('detail.kpi.hit'), value: formatPercent(s.hit_ratio_24h) },
    { label: t('detail.kpi.requests'), value: formatCount(s.requests_24h) },
    { label: t('detail.kpi.served'), value: formatBytes(s.bytes_served_24h) },
    { label: t('detail.kpi.activity'), component: TimeCell, props: { value: s.last_activity_at, placeholder: t('repos.never') } },
  ]
})

function onSaved(next: Repository): void {
  override.value = next
  repos.upsert(next)
}
</script>

<style scoped>
.detail__type {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: var(--fs-12);
  font-weight: 550;
  color: var(--text-secondary);
  letter-spacing: 0;
}

.detail__caption {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 14px;
}

.detail__caption :deep(.copy-field) {
  max-width: min(520px, 100%);
}

.detail__kpis {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow-card);
}

.detail__quota {
  margin-top: 12px;
  padding: 12px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow-card);
}

.detail__kpi {
  padding: 12px 16px;
  border-left: 1px solid var(--border);
  min-width: 0;
}

.detail__kpi:first-child {
  border-left: 0;
}

.detail__kpi-label {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.detail__kpi-value {
  margin-top: 2px;
  font-size: var(--fs-20);
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.detail__tabs {
  margin: 16px 0 12px;
  border-bottom: 1px solid var(--border);
  color: var(--text-secondary);
}

@media (max-width: 1023px) {
  .detail__kpis {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .detail__kpi:nth-child(4) {
    border-left: 0;
  }

  .detail__kpi:nth-child(n + 4) {
    border-top: 1px solid var(--border);
  }
}

@media (max-width: 599px) {
  .detail__kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .detail__kpi:nth-child(odd) {
    border-left: 0;
  }

  .detail__kpi:nth-child(4) {
    border-left: 1px solid var(--border);
  }

  .detail__kpi:nth-child(n + 3) {
    border-top: 1px solid var(--border);
  }

  .detail__kpi-value {
    font-size: var(--fs-16);
  }
}
</style>
