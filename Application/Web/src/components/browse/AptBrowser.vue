<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- apt browse: suites (components, architectures, refresh) → packages per suite/arch with cache state. -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="nav({ suite: null, arch: null, q: null })" /></template>
    <template v-if="suite" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.apt.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
      <q-select v-model="arch" dense outlined emit-value map-options options-dense :options="archOptions" :label="t('browse.apt.arch')" class="arch-select" />
      <q-toggle v-model="cachedOnly" dense :label="t('browse.apt.cachedOnly')" />
    </template>

    <div v-if="!suite" class="suites">
      <q-skeleton v-if="suites.loading.value && !suites.data.value" height="140px" />
      <button v-for="s in suites.data.value ?? []" :key="s.suite" type="button" class="suite" @click="nav({ suite: s.suite })">
        <div class="suite__head">
          <span class="mono suite__name">{{ s.suite }}</span>
          <span class="text-caption">{{ s.origin }}</span>
        </div>
        <div class="suite__chips">
          <span v-for="c in s.components" :key="c" class="chip">{{ c }}</span>
        </div>
        <div class="suite__chips">
          <span v-for="a in s.architectures" :key="a" class="chip chip--arch mono">{{ a }}</span>
        </div>
        <div class="suite__stats">
          <span>{{ t('browse.apt.packagesCached', { cached: formatCount(s.cached_package_count ?? 0), total: formatCount(s.package_count ?? 0) }) }}</span>
          <span>{{ t('browse.apt.refreshed') }} <TimeCell :value="s.last_refreshed_at" /></span>
        </div>
      </button>
      <EmptyState v-if="suites.data.value && !suites.data.value.length" compact :title="t('browse.apt.noSuites')" :text="t('browse.apt.noSuitesText')" />
    </div>

    <q-table
      v-else
      class="ws-table"
      flat
      :rows="packages.data.value?.items ?? []"
      :columns="columns"
      row-key="filename"
      :loading="packages.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
    >
      <template #body-cell-name="{ row }">
        <q-td>
          <div class="mono pkg">{{ row.name }}</div>
          <div class="file mono" :title="row.filename">{{ row.filename }}</div>
        </q-td>
      </template>
      <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size" /></q-td></template>
      <template #body-cell-cached="{ row }">
        <q-td>
          <template v-if="row.cached">
            <CacheBadge state="HIT" />
            <span v-if="row.verified === false" class="ws-badge tone-degraded unverified">{{ t('browse.apt.unverified') }}<q-tooltip>{{ t('browse.apt.unverifiedHelp') }}</q-tooltip></span>
          </template>
          <span v-else class="text-faint">{{ t('browse.notCached') }}</span>
        </q-td>
      </template>
      <template #body-cell-last="{ row }"><q-td><TimeCell :value="row.last_downloaded_at" :placeholder="t('repos.never')" /></q-td></template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref } from 'vue'

import { api, call } from '@/api/client'
import type { AptPackage, Repository } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useQueryNavigate, useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const suite = useQueryParam('suite')
const archParam = useQueryParam('arch')
const nav = useQueryNavigate()
const search = ref<string | null>(null)
const cachedOnly = ref(false)

const suites = useAsync(() => call(api.GET('/repositories/{name}/apt/suites', { params: { path: { name: props.repo.name } } })))
const current = computed(() => suites.data.value?.find((s) => s.suite === suite.value))
const archOptions = computed(() => (current.value?.architectures ?? []).filter((a) => a !== 'all').map((a) => ({ label: a, value: a })))
const arch = computed({
  get: () => archParam.value ?? archOptions.value[0]?.value ?? null,
  set: (v: string | null) => nav({ arch: v }, true),
})

const packages = useAsync(
  async () =>
    suite.value
      ? call(
          api.GET('/repositories/{name}/apt/packages', {
            params: { path: { name: props.repo.name }, query: { suite: suite.value, arch: arch.value ?? undefined, q: search.value || undefined, cached: cachedOnly.value || undefined, limit: 500 } },
          }),
        )
      : null,
  [suite, arch, search, cachedOnly],
)

const crumbs = computed(() => [{ label: t('browse.apt.suites') }, ...(suite.value ? [{ label: suite.value, mono: true }] : [])])

const columns = computed<QTableColumn<AptPackage>[]>(() => [
  { name: 'name', label: t('browse.apt.package'), field: 'name', align: 'left', sortable: true },
  { name: 'version', label: t('browse.apt.version'), field: 'version', align: 'left', classes: 'mono', sortable: true },
  { name: 'arch', label: t('browse.apt.arch'), field: 'architecture', align: 'left', classes: 'gt-xs mono', headerClasses: 'gt-xs' },
  { name: 'section', label: t('browse.apt.section'), field: (r) => r.section ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size ?? 0, align: 'right', sortable: true },
  { name: 'cached', label: t('browse.cache'), field: 'cached', align: 'left', sortable: true },
  { name: 'last', label: t('browse.lastDownloaded'), field: (r) => r.last_downloaded_at ?? '', align: 'left', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
])
</script>

<style scoped>
.browse-search {
  width: 220px;
}

.arch-select {
  width: 120px;
}

.suites {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
  padding: 4px 16px 16px;
}

.suite {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--text-primary);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color var(--dur) var(--ease-out);
}

.suite:hover {
  border-color: var(--q-primary);
}

.suite__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.suite__name {
  font-size: var(--fs-16);
  font-weight: 650;
}

.suite__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.chip {
  padding: 1px 7px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--surface-subtle);
  font-size: 11px;
  color: var(--text-secondary);
}

.chip--arch {
  background: transparent;
}

.suite__stats {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.pkg {
  font-weight: 600;
}

.file {
  max-width: 340px;
  font-size: 11px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.unverified {
  margin-left: 6px;
  height: 20px;
  font-size: 11px;
}
</style>
