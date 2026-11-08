<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Bytes per repository: sortable, filterable (filter and sort live in the URL), share of the total as a bar + %. -->
<template>
  <PageCard :title="t('storage.table.title')" :caption="t('storage.table.caption')" flush>
    <template #actions>
      <q-input v-model="needle" dense outlined clearable debounce="200" :placeholder="t('storage.table.search')" :aria-label="t('storage.table.search')" class="st-table__search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>
    <q-table
      v-model:pagination="pagination"
      class="ws-table"
      flat
      :rows="shown"
      :columns="columns"
      row-key="repository"
      :loading="loading"
      :grid="$q.screen.lt.sm"
      hide-pagination
      :rows-per-page-options="[0]"
    >
      <template #body-cell-repository="{ row }">
        <q-td>
          <div class="st-table__name">
            <FormatIcon :mark="repos.markFor(row.repository, row.format)" :size="18" brand />
            <router-link :to="{ name: 'repository', params: { name: row.repository } }" class="st-table__link">{{ row.repository }}</router-link>
          </div>
        </q-td>
      </template>
      <template #body-cell-size="{ row }">
        <q-td class="text-right"><SizeCell :bytes="row.bytes" /></q-td>
      </template>
      <template #body-cell-share="{ row }">
        <q-td>
          <div class="st-table__share">
            <div class="st-table__track" aria-hidden="true"><div class="st-table__bar" :style="{ width: `${Math.max(row.share > 0 ? 1.5 : 0, row.share * 100)}%`, background: formatColor(row.format) }" /></div>
            <span class="num">{{ formatPercent(row.share) }}</span>
          </div>
        </q-td>
      </template>
      <template #body-cell-average="{ row }">
        <q-td class="text-right"><SizeCell :bytes="row.average" /></q-td>
      </template>
      <template #body-cell-activity="{ row }">
        <q-td class="text-right">
          <q-btn flat dense round size="sm" :icon="iconActivity" :to="{ name: 'activity', query: { repo: row.repository } }" :aria-label="t('storage.table.viewActivity', { name: row.repository })">
            <q-tooltip>{{ t('storage.table.viewActivity', { name: row.repository }) }}</q-tooltip>
          </q-btn>
        </q-td>
      </template>

      <template #item="{ row }">
        <div class="st-table__card">
          <div class="st-table__name">
            <FormatIcon :mark="repos.markFor(row.repository, row.format)" :size="20" brand />
            <router-link :to="{ name: 'repository', params: { name: row.repository } }" class="st-table__link">{{ row.repository }}</router-link>
            <q-space />
            <SizeCell :bytes="row.bytes" />
          </div>
          <div class="st-table__share q-mt-sm">
            <div class="st-table__track" aria-hidden="true"><div class="st-table__bar" :style="{ width: `${row.share * 100}%`, background: formatColor(row.format) }" /></div>
            <span class="num">{{ formatPercent(row.share) }}</span>
          </div>
          <div class="st-table__meta">{{ formatCount(row.artifacts) }} {{ t('storage.table.artifacts').toLowerCase() }} · {{ formatLabel(row.format) }}</div>
        </div>
      </template>

      <template #no-data>
        <EmptyState class="full-width" compact :icon="iconStorage" :title="needle ? t('storage.table.noMatch') : t('storage.table.none')" />
      </template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import EmptyState from '@/components/EmptyState.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import { useQueryNavigate } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatColor, formatLabel } from '@/utils/domain'
import { formatCount, formatPercent } from '@/utils/format'
import { iconActivity, iconSearch, iconStorage } from '@/utils/icons'
import { filterRows, type RepoStorageRow } from '@/utils/storageStats'

const props = withDefaults(defineProps<{ rows: RepoStorageRow[]; loading?: boolean }>(), { loading: false })

const repos = useRepositoriesStore()
const route = useRoute()
const navigate = useQueryNavigate()

const SORTS = ['repository', 'format', 'size', 'share', 'artifacts', 'average'] as const
const needle = computed<string | null>({
  get: () => (typeof route.query.q === 'string' ? route.query.q : null),
  set: (value) => navigate({ q: value || null }, true),
})
const pagination = computed({
  get: () => {
    const sort = typeof route.query.sort === 'string' && (SORTS as readonly string[]).includes(route.query.sort) ? route.query.sort : 'size'
    return { rowsPerPage: 0, sortBy: sort, descending: route.query.dir !== 'asc' }
  },
  set: (value: { sortBy?: string | null; descending?: boolean }) => {
    navigate({ sort: value.sortBy && value.sortBy !== 'size' ? value.sortBy : null, dir: value.descending === false ? 'asc' : null }, true)
  },
})

const shown = computed(() => filterRows(props.rows, needle.value))

const columns = computed<QTableColumn<RepoStorageRow>[]>(() => [
  { name: 'repository', label: t('storage.table.name'), field: 'repository', align: 'left', sortable: true },
  { name: 'format', label: t('storage.table.format'), field: 'format', format: (v: RepoStorageRow['format']) => formatLabel(v), align: 'left', sortable: true, classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'size', label: t('storage.table.size'), field: 'bytes', align: 'right', sortable: true },
  { name: 'share', label: t('storage.table.share'), field: 'share', align: 'left', sortable: true, style: 'width: 220px' },
  { name: 'artifacts', label: t('storage.table.artifacts'), field: 'artifacts', format: (v: number) => formatCount(v), align: 'right', sortable: true },
  { name: 'average', label: t('storage.table.average'), field: (r) => r.average ?? -1, align: 'right', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'activity', label: '', field: 'repository', align: 'right' },
])
</script>

<style scoped>
.st-table__search {
  width: 240px;
}

.st-table__name {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.st-table__link {
  font-weight: 600;
  color: var(--text-primary);
  text-decoration: none;
}

.st-table__link:hover {
  color: var(--q-primary);
  text-decoration: underline;
}

.st-table__share {
  display: flex;
  align-items: center;
  gap: 10px;
}

.st-table__track {
  flex: 1;
  min-width: 60px;
  height: 6px;
  border-radius: 999px;
  background: var(--chart-track);
  overflow: hidden;
}

.st-table__bar {
  height: 100%;
  border-radius: 999px;
}

.st-table__share .num {
  width: 48px;
  text-align: right;
}

.st-table__card {
  width: calc(100% - 24px);
  margin: 0 12px 8px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.st-table__meta {
  margin-top: 6px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

@media (max-width: 599px) {
  .st-table__search {
    width: 100%;
  }
}
</style>
