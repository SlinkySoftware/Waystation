<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Repositories (UI_DESIGN §4.3): the backbone list with filters, search and the create flow. Cards on phones. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.repositories')" :caption="caption">
      <template #actions>
        <q-btn v-if="auth.can('admin')" flat no-caps :icon="iconDownload" :label="t('nexusImport.open')" :to="{ name: 'import-nexus' }" />
        <q-btn v-if="auth.can('repositories.create')" unelevated no-caps color="primary" :icon="iconAdd" :label="t('repos.create')" @click="creating = true" />
      </template>
    </PageHeader>

    <q-card class="repos">
      <div class="repos__toolbar">
        <q-input v-model="q" dense outlined clearable debounce="200" :placeholder="t('repos.search')" class="repos__search" :aria-label="t('repos.search')">
          <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
        </q-input>
        <q-select v-model="format" dense outlined clearable emit-value map-options options-dense :options="formatOptions" :label="t('repos.format')" class="repos__filter" />
        <q-select v-model="type" dense outlined clearable emit-value map-options options-dense :options="typeOptions" :label="t('repos.type')" class="repos__filter" />
        <q-select v-model="status" dense outlined clearable emit-value map-options options-dense :options="statusOptions" :label="t('repos.status')" class="repos__filter" />
        <q-space />
        <q-btn-toggle
          v-if="$q.screen.gt.xs"
          v-model="view"
          dense
          no-caps
          unelevated
          toggle-color="primary"
          class="repos__view"
          :aria-label="t('ui.repoList.view')"
          :options="viewOptions"
        >
          <template #table><q-icon :name="iconTableView" size="18px" /><span class="visually-hidden">{{ t('ui.repoList.table') }}</span><q-tooltip>{{ t('ui.repoList.table') }}</q-tooltip></template>
          <template #grid><q-icon :name="iconGridView" size="18px" /><span class="visually-hidden">{{ t('ui.repoList.grid') }}</span><q-tooltip>{{ t('ui.repoList.grid') }}</q-tooltip></template>
        </q-btn-toggle>
      </div>

      <div v-if="gridView && rows.length" class="repos__grid" role="list" :aria-label="t('nav.repositories')">
        <RepoCard v-for="row in sortedRows" :key="row.name" :repo="row" rich role="listitem" />
      </div>

      <q-table
        v-else
        class="ws-table"
        flat
        :rows="rows"
        :columns="columns"
        row-key="name"
        :loading="list.loading.value && !list.data.value"
        :grid="$q.screen.lt.sm"
        :pagination="{ rowsPerPage: 0, sortBy: 'name' }"
        hide-pagination
        :no-data-label="t('repos.none')"
        @row-click="(_e: Event, row: Repository) => open(row)"
      >
        <template #body-cell-name="{ row }">
          <q-td class="cursor-pointer">
            <div class="repos__name">
              <FormatIcon :mark="repoMark(row)" :size="20" brand />
              <div class="repos__name-text">
                <router-link :to="{ name: 'repository', params: { name: row.name } }" class="repos__link" @click.stop>{{ row.name }}</router-link>
                <ManagedBadge :item="row" class="q-ml-xs" />
                <div class="repos__desc">{{ row.description }}</div>
              </div>
            </div>
          </q-td>
        </template>
        <template #body-cell-type="{ row }">
          <q-td><span class="repos__type"><q-icon :name="typeMeta(row.type).icon" size="15px" />{{ typeMeta(row.type).label }}</span></q-td>
        </template>
        <template #body-cell-status="{ row }">
          <q-td><StatusPill :status="row.status" /></q-td>
        </template>
        <template #body-cell-url="{ row }">
          <q-td @click.stop>
            <q-btn flat dense round size="sm" :icon="iconLink" :aria-label="t('common.copyWhat', { what: row.url })" @click="copy(row.url, t('repos.url'))">
              <q-tooltip>{{ row.url }}</q-tooltip>
            </q-btn>
          </q-td>
        </template>
        <template #body-cell-size="{ row }">
          <q-td class="text-right">
            <SizeCell :bytes="row.stats.size_bytes" />
            <QuotaMeter v-if="row.quota" :quota="row.quota" compact class="repos__quota" />
          </q-td>
        </template>
        <template #body-cell-hit="{ row }">
          <q-td class="text-right">{{ formatPercent(row.stats.hit_ratio_24h) }}</q-td>
        </template>
        <template #body-cell-requests="{ row }">
          <q-td>
            <div class="repos__spark">
              <Sparkline :values="row.stats.requests_spark_24h" :height="22" />
              <span class="num">{{ formatCount(row.stats.requests_24h) }}</span>
            </div>
          </q-td>
        </template>
        <template #body-cell-activity="{ row }">
          <q-td><TimeCell :value="row.stats.last_activity_at" :placeholder="t('repos.never')" /></q-td>
        </template>

        <template #item="{ row }">
          <RepoCard :repo="row" class="repos__card" />
        </template>

        <template #no-data>
          <EmptyState class="full-width" :illustration="filtered ? 'search' : 'repositories'" :title="filtered ? t('repos.noMatch') : t('repos.emptyTitle')" :text="filtered ? t('repos.noMatchText') : auth.can('repositories.create') ? t('repos.emptyText') : t('repos.emptyNoAccessText')">
            <q-btn v-if="filtered" flat no-caps :label="t('repos.clearFilters')" @click="clearFilters" />
            <q-btn v-else-if="auth.can('repositories.create')" unelevated no-caps color="primary" :label="t('repos.create')" @click="creating = true" />
          </EmptyState>
        </template>
      </q-table>
    </q-card>

    <CreateRepositoryDialog v-model="creating" @created="onCreated" />
  </q-page>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, call } from '@/api/client'
import type { Format, Repository, RepositoryStatus, RepositoryType } from '@/api/types'
import Sparkline from '@/components/charts/Sparkline.vue'
import EmptyState from '@/components/EmptyState.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import ManagedBadge from '@/components/ManagedBadge.vue'
import PageHeader from '@/components/PageHeader.vue'
import CreateRepositoryDialog from '@/components/repositories/CreateRepositoryDialog.vue'
import QuotaMeter from '@/components/repositories/QuotaMeter.vue'
import RepoCard from '@/components/repositories/RepoCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import StatusPill from '@/components/StatusPill.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatLabel, LISTED_FORMATS, repoMark, statusMeta, typeMeta } from '@/utils/domain'
import { formatCount, formatPercent } from '@/utils/format'
import { iconAdd, iconDownload, iconGridView, iconLink, iconSearch, iconTableView } from '@/utils/icons'
import { readString, writeString } from '@/utils/storage'

const $q = useQuasar()
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const repos = useRepositoriesStore()
const copy = useCopy()

// Filters live in the URL so a filtered list can be shared and survives reload.
const str = (v: unknown) => (typeof v === 'string' && v ? v : null)
const q = ref<string | null>(str(route.query.q))
const format = ref<Format | null>(str(route.query.format) as Format | null)
const type = ref<RepositoryType | null>(str(route.query.type) as RepositoryType | null)
const status = ref<RepositoryStatus | null>(str(route.query.status) as RepositoryStatus | null)
const creating = ref(route.query.create === '1')
// Table or card grid (T-U04): `?view=grid` wins, else the last choice on this browser.
const VIEW_KEY = 'ws.repos.view'
const view = ref<'table' | 'grid'>((str(route.query.view) ?? readString(VIEW_KEY)) === 'grid' ? 'grid' : 'table')
const gridView = computed(() => view.value === 'grid' && !$q.screen.lt.sm)
const viewOptions = [
  { value: 'table', slot: 'table' },
  { value: 'grid', slot: 'grid' },
]
watch(view, (v) => writeString(VIEW_KEY, v))

watch([q, format, type, status, view], () => {
  const query = Object.fromEntries(
    Object.entries({ q: q.value, format: format.value, type: type.value, status: status.value, view: view.value === 'grid' ? 'grid' : null }).filter(([, v]) => v),
  )
  void router.replace({ query: query as Record<string, string> })
})

const list = useAsync(
  () =>
    call(
      api.GET('/repositories', {
        params: { query: { q: q.value || undefined, format: format.value ?? undefined, type: type.value ?? undefined, status: status.value ?? undefined, limit: 500 } },
      }),
    ),
  [q, format, type, status],
)
const rows = computed(() => list.data.value?.items ?? [])
const sortedRows = computed(() => [...rows.value].sort((a, b) => a.name.localeCompare(b.name)))
const filtered = computed(() => Boolean(q.value || format.value || type.value || status.value))
const caption = computed(() => (list.data.value ? t('repos.caption', { count: list.data.value.total }) : t('common.loading')))

const formatOptions = LISTED_FORMATS.map((f) => ({ label: formatLabel(f), value: f }))
const typeOptions = (['proxy', 'hosted', 'group'] as RepositoryType[]).map((ty) => ({ label: typeMeta(ty).label, value: ty }))
const statusOptions = (['ok', 'degraded', 'blocked', 'offline'] as RepositoryStatus[]).map((s) => ({ label: statusMeta(s).label, value: s }))

const columns = computed<QTableColumn<Repository>[]>(() => [
  { name: 'name', label: t('repos.col.name'), field: 'name', align: 'left', sortable: true },
  { name: 'type', label: t('repos.col.type'), field: 'type', align: 'left', sortable: true },
  { name: 'status', label: t('repos.col.status'), field: 'status', align: 'left', sortable: true },
  { name: 'url', label: t('repos.col.url'), field: 'url', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'size', label: t('repos.col.size'), field: (r) => r.stats.size_bytes, align: 'right', sortable: true },
  { name: 'artifacts', label: t('repos.col.artifacts'), field: (r) => r.stats.artifact_count, format: (v: number) => formatCount(v), align: 'right', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'hit', label: t('repos.col.hit'), field: (r) => r.stats.hit_ratio_24h ?? -1, align: 'right', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'requests', label: t('repos.col.requests'), field: (r) => r.stats.requests_24h, align: 'left', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'activity', label: t('repos.col.activity'), field: (r) => r.stats.last_activity_at ?? '', align: 'left', sortable: true },
])

function open(row: Repository): void {
  void router.push({ name: 'repository', params: { name: row.name } })
}

function clearFilters(): void {
  q.value = null
  format.value = null
  type.value = null
  status.value = null
}

function onCreated(repo: Repository): void {
  repos.upsert(repo)
  void router.push({ name: 'repository', params: { name: repo.name } })
}
</script>

<style scoped>
.repos__quota {
  margin-top: 4px;
  margin-left: auto;
  max-width: 120px;
}

.repos {
  overflow: hidden;
}

.repos__toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.repos__search {
  flex: 1 1 260px;
  max-width: 420px;
}

.repos__filter {
  flex: 0 1 160px;
  min-width: 100px;
}

@media (max-width: 599px) {
  .repos__filter {
    flex: 1 1 0;
  }

  .repos__card .repos__desc {
    max-width: none;
  }
}

.repos__name {
  font-variant-numeric: normal;
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.repos__name-text {
  min-width: 0;
  flex: 1;
}

.repos__link {
  font-weight: 600;
  font-size: var(--fs-14);
  color: var(--text-primary);
  text-decoration: none;
}

.repos__link:hover {
  color: var(--q-primary);
  text-decoration: underline;
}

.repos__desc {
  max-width: 220px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.repos__type {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: var(--fs-12);
  font-weight: 550;
  color: var(--text-secondary);
}

.repos__url {
  max-width: 220px;
}

.repos__spark {
  display: flex;
  align-items: center;
  gap: 10px;
}

.repos__spark :deep(.sparkline) {
  width: 60px;
}

.repos__card {
  width: calc(100% - 24px);
  margin: 0 12px 8px;
}

.repos__view {
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
}

.repos__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
  gap: 12px;
  padding: 16px;
}
</style>
