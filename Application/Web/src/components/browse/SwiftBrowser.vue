<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Swift Package Registry browse: scopes → packages → package page (dependency snippet, releases newest first by
     SemVer: tools version, published, size, sha256 checksum, signature, source (published or the Git tag it was
     built from), publisher; removed releases stay listed). A proxy lists the releases clients have asked for. -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="go" /></template>
    <template v-if="!pkg" #actions>
      <q-input
        v-model="search"
        dense
        outlined
        clearable
        debounce="200"
        :placeholder="scope ? t('browse.swift.searchPackages') : t('browse.swift.search')"
        class="browse-search"
      >
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="scope && pkg">
      <div v-if="releases.data.value" class="pkg">
        <div class="pkg__head">
          <div class="col">
            <div class="pkg__name mono">{{ releases.data.value.package.id }}</div>
            <div v-if="releases.data.value.package.description" class="pkg__desc">{{ releases.data.value.package.description }}</div>
            <div class="pkg__meta">
              <span v-if="releases.data.value.package.latest_version">
                {{ t('browse.swift.latest') }} <strong class="mono">{{ releases.data.value.package.latest_version }}</strong>
              </span>
              <span>{{ t('browse.swift.releaseCount', { count: releases.data.value.package.release_count }) }}</span>
              <span v-if="releases.data.value.package.removed_count">{{ t('browse.swift.removedCount', { count: releases.data.value.package.removed_count }) }}</span>
              <span v-if="releases.data.value.package.repository_url" class="mono">{{ releases.data.value.package.repository_url }}</span>
            </div>
          </div>
          <div v-if="releases.data.value.package.latest_version" class="pkg__install">
            <div class="section-label">{{ t('browse.swift.dependency') }}</div>
            <CopyField :value="`.package(id: &quot;${releases.data.value.package.id}&quot;, from: &quot;${releases.data.value.package.latest_version}&quot;)`" />
          </div>
        </div>
        <div v-if="repo.type === 'proxy'" class="pkg__note">{{ t('browse.swift.pinned') }}</div>
        <q-table class="ws-table" flat :rows="releases.data.value.items" :columns="columns" row-key="version" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('browse.empty')">
          <template #body-cell-version="{ row }">
            <q-td>
              <span class="mono ver" :class="{ 'ver--removed': row.state === 'removed' }">{{ row.version }}</span>
              <span v-if="row.state === 'removed'" class="ws-badge tone-error badge">{{ t('browse.swift.removed') }}</span>
              <span v-else-if="row.prerelease" class="ws-badge tone-offline badge">{{ t('browse.swift.prerelease') }}</span>
            </q-td>
          </template>
          <template #body-cell-published="{ row }"><q-td><TimeCell :value="row.published_at ?? null" placeholder="" /></q-td></template>
          <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell :bytes="row.size_bytes ?? null" /></q-td></template>
          <template #body-cell-sha="{ row }">
            <q-td class="mono digest">{{ row.checksum.slice(0, 12) }}<q-tooltip>sha256:{{ row.checksum }}</q-tooltip></q-td>
          </template>
          <template #body-cell-signed="{ row }">
            <q-td>
              <span v-if="row.signed">{{ t('browse.swift.signed') }}<q-tooltip v-if="row.signer">{{ row.signer }}</q-tooltip></span>
              <span v-else class="text-faint">{{ t('browse.swift.unsigned') }}</span>
            </q-td>
          </template>
          <template #body-cell-source="{ row }">
            <q-td>
              <span v-if="row.source === 'scm'" class="mono">{{ t('browse.swift.sourceScm', { tag: row.tag ?? '' }) }}<q-tooltip v-if="row.commit">{{ row.commit }}</q-tooltip></span>
              <span v-else>{{ t('browse.swift.sourceHosted') }}</span>
            </q-td>
          </template>
        </q-table>
      </div>
      <div v-else-if="releases.error.value" class="q-pa-md text-negative">{{ errorMessage(releases.error.value) }}</div>
      <q-skeleton v-else height="300px" class="q-ma-md" />
    </template>

    <q-table
      v-else-if="scope"
      class="ws-table"
      flat
      :rows="packages.data.value?.items ?? []"
      :columns="packageColumns"
      row-key="name"
      :loading="packages.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: SwiftPackage) => (pkg = row.name)"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <div class="mono pkg-name">{{ row.name }}</div>
          <div v-if="row.description" class="pkg-desc">{{ row.description }}</div>
        </q-td>
      </template>
    </q-table>

    <q-table
      v-else
      class="ws-table"
      flat
      :rows="scopes.data.value?.items ?? []"
      :columns="scopeColumns"
      row-key="scope"
      :loading="scopes.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: SwiftScope) => open(row.scope)"
    >
      <template #body-cell-scope="{ row }">
        <q-td class="cursor-pointer mono pkg-name">{{ row.scope }}</q-td>
      </template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { Repository, SwiftPackage, SwiftRelease, SwiftScope } from '@/api/types'
import CopyField from '@/components/CopyField.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const scope = useQueryParam('scope')
const pkg = useQueryParam('package')
const search = ref<string | null>(null)

const scopes = useAsync(
  async () =>
    scope.value
      ? null
      : call(api.GET('/repositories/{name}/swift/scopes', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [scope, search],
)
const packages = useAsync(
  async () =>
    scope.value && !pkg.value
      ? call(
          api.GET('/repositories/{name}/swift/packages', {
            params: { path: { name: props.repo.name }, query: { scope: scope.value, q: search.value || undefined, limit: 200 } },
          }),
        )
      : null,
  [scope, pkg, search],
)
const releases = useAsync(
  async () =>
    scope.value && pkg.value
      ? call(
          api.GET('/repositories/{name}/swift/releases', {
            params: { path: { name: props.repo.name }, query: { scope: scope.value, package: pkg.value, limit: 200 } },
          }),
        )
      : null,
  [scope, pkg],
)

watch(scope, () => (search.value = null))

function open(value: string): void {
  pkg.value = null
  scope.value = value
}

function go(index: number): void {
  if (index === 0) {
    pkg.value = null
    scope.value = null
  } else {
    pkg.value = null
  }
}

const crumbs = computed(() => [
  { label: t('browse.swift.scopes') },
  ...(scope.value ? [{ label: scope.value, mono: true }] : []),
  ...(scope.value && pkg.value ? [{ label: pkg.value, mono: true }] : []),
])

const scopeColumns = computed<QTableColumn<SwiftScope>[]>(() => [
  { name: 'scope', label: t('browse.swift.scope'), field: 'scope', align: 'left', sortable: true },
  { name: 'packages', label: t('browse.swift.packages'), field: 'package_count', align: 'right', sortable: true },
])

const packageColumns = computed<QTableColumn<SwiftPackage>[]>(() => [
  { name: 'name', label: t('browse.swift.package'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.swift.latest'), field: (r) => r.latest_version ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'releases', label: t('browse.swift.releases'), field: 'release_count', align: 'right', sortable: true },
])

const columns = computed<QTableColumn<SwiftRelease>[]>(() => [
  { name: 'version', label: t('browse.swift.version'), field: 'version', align: 'left' },
  { name: 'tools', label: t('browse.swift.tools'), field: (r) => r.tools_version ?? '', align: 'left', classes: 'mono gt-sm', headerClasses: 'gt-sm' },
  { name: 'published', label: t('browse.swift.published'), field: (r) => r.published_at ?? '', align: 'left' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size_bytes ?? 0, align: 'right' },
  { name: 'sha', label: t('browse.swift.checksum'), field: 'checksum', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'signed', label: t('browse.swift.signed'), field: 'signed', align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
  { name: 'source', label: t('browse.swift.source'), field: 'source', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'by', label: t('browse.swift.publishedBy'), field: (r) => r.published_by ?? '', align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
])
</script>

<style scoped>
.browse-search {
  width: 260px;
}

.pkg-name {
  font-weight: 600;
}

.pkg-desc {
  max-width: 520px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pkg__head {
  display: flex;
  flex-wrap: wrap;
  gap: 16px 24px;
  padding: 16px;
  border-top: 1px solid var(--border);
}

.pkg__name {
  font-size: var(--fs-16);
  font-weight: 600;
}

.pkg__desc {
  margin-top: 4px;
  font-size: var(--fs-14);
}

.pkg__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin-top: 6px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.pkg__install {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 280px;
}

.pkg__note {
  padding: 0 16px 12px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.ver {
  font-weight: 600;
}

.ver--removed {
  text-decoration: line-through;
  color: var(--text-secondary);
}

.badge {
  margin-left: 8px;
  height: 18px;
  font-size: 11px;
}

.digest {
  color: var(--text-secondary);
}
</style>
