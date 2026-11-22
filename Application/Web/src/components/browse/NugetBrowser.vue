<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- NuGet browse: packages → package page (install snippet, versions newest first as the server orders them, with
     pre-release and unlisted badges, cache state, size, publish date and download link; hosted: who pushed it and
     unlist / relist). Groups list their members' packages. -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="pkg = null" /></template>
    <template v-if="!pkg" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.nuget.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="pkg">
      <div v-if="versions.data.value" class="pkg">
        <div class="pkg__head">
          <div class="col">
            <div class="pkg__name mono">{{ versions.data.value.package.id }}</div>
            <div v-if="versions.data.value.package.description" class="pkg__desc">{{ versions.data.value.package.description }}</div>
            <div class="pkg__meta">
              <span v-if="versions.data.value.package.latest_version">{{ t('browse.nuget.latest') }} <strong class="mono">{{ versions.data.value.package.latest_version }}</strong></span>
              <span>{{ t('browse.nuget.versionCount', { count: versions.data.value.package.version_count }) }}</span>
              <span v-if="versions.data.value.package.authors">{{ t('browse.nuget.by', { authors: versions.data.value.package.authors }) }}</span>
            </div>
          </div>
          <div v-if="versions.data.value.package.latest_version" class="pkg__install">
            <div class="section-label">{{ t('browse.nuget.install') }}</div>
            <CopyField :value="`dotnet add package ${versions.data.value.package.id} --version ${versions.data.value.package.latest_version}`" />
          </div>
        </div>

        <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" row-key="version" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('browse.empty')">
          <template #body-cell-version="{ row }">
            <q-td>
              <span class="mono ver" :class="{ 'ver--unlisted': !row.listed }">{{ row.version }}</span>
              <span v-if="row.prerelease" class="ws-badge tone-offline badge">{{ t('browse.nuget.prerelease') }}</span>
              <span v-if="!row.listed" class="ws-badge tone-degraded badge">
                {{ t('browse.nuget.unlisted') }}
                <q-tooltip max-width="320px">{{ t('browse.nuget.unlistedHelp') }}</q-tooltip>
              </span>
              <div v-if="row.uploaded_by" class="by">{{ t('browse.nuget.uploadedBy', { user: row.uploaded_by }) }}</div>
            </q-td>
          </template>
          <template #body-cell-published="{ row }"><q-td><TimeCell :value="row.published_at" placeholder="" /></q-td></template>
          <template #body-cell-size="{ row }">
            <q-td class="text-right"><SizeCell v-if="row.size_bytes !== null && row.size_bytes !== undefined" :bytes="row.size_bytes" /></q-td>
          </template>
          <template #body-cell-cached="{ row }">
            <q-td><CacheBadge v-if="row.cached" state="HIT" /><span v-else class="text-faint">{{ t('browse.notCached') }}</span></q-td>
          </template>
          <template #body-cell-actions="{ row }">
            <q-td auto-width class="actions">
              <q-btn
                v-if="canList"
                flat
                dense
                no-caps
                size="sm"
                :label="row.listed ? t('browse.nuget.unlist') : t('browse.nuget.relist')"
                :loading="listing === row.version"
                :data-test="`nuget-listing-${row.version}`"
                @click="setListed(row.version, !row.listed)"
              />
              <q-btn flat dense round size="sm" :icon="iconCopy" :aria-label="t('browse.copyUrl')" @click="copy(row.download_url, t('browse.url'))">
                <q-tooltip>{{ t('browse.copyUrl') }}</q-tooltip>
              </q-btn>
              <q-btn flat dense round size="sm" type="a" :href="row.download_url" :icon="iconDownload" :aria-label="t('browse.nuget.download')">
                <q-tooltip>{{ t('browse.nuget.download') }}</q-tooltip>
              </q-btn>
            </q-td>
          </template>
        </q-table>
      </div>
      <div v-else-if="versions.error.value" class="q-pa-md text-negative">{{ errorMessage(versions.error.value) }}</div>
      <q-skeleton v-else height="300px" class="q-ma-md" />
    </template>

    <q-table
      v-else
      class="ws-table"
      flat
      :rows="packages.data.value?.items ?? []"
      :columns="packageColumns"
      row-key="name"
      :loading="packages.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: NugetPackage) => (pkg = row.name)"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <div class="mono pkg-name">{{ row.id }}</div>
          <div class="pkg-desc">
            <span v-if="row.authors">{{ row.authors }}</span><span v-if="row.authors && row.description"> · </span>{{ row.description }}
          </div>
        </q-td>
      </template>
      <template #body-cell-updated="{ row }"><q-td><TimeCell :value="row.updated_at ?? null" placeholder="" /></q-td></template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { NugetPackage, NugetVersion, Repository } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy, useNotify } from '@/composables/useNotify'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { iconCopy, iconDownload, iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const notify = useNotify()
const copy = useCopy()
const auth = useAuthStore()
const pkg = useQueryParam('package')
const search = ref<string | null>(null)
/** Unlist / relist: hosted repositories only, for signed-in users (the server checks the write permission). */
const canList = computed(() => props.repo.type === 'hosted' && auth.signedIn)

const packages = useAsync(
  () => call(api.GET('/repositories/{name}/nuget/packages', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const versions = useAsync(
  async () => (pkg.value ? call(api.GET('/repositories/{name}/nuget/versions', { params: { path: { name: props.repo.name }, query: { package: pkg.value, limit: 200 } } })) : null),
  [pkg],
)

const crumbs = computed(() => [{ label: t('browse.nuget.packages') }, ...(pkg.value ? [{ label: versions.data.value?.package.id ?? pkg.value, mono: true }] : [])])

const listing = ref<string | null>(null)

async function setListed(version: string, listed: boolean): Promise<void> {
  const id = versions.data.value?.package.id ?? pkg.value
  if (!id) return
  listing.value = version
  try {
    await call(api.POST('/repositories/{name}/nuget/listing', { params: { path: { name: props.repo.name } }, body: { package: id, version, listed } }))
    notify.success(listed ? t('browse.nuget.relistedDone', { version }) : t('browse.nuget.unlistedDone', { version }))
    versions.reload()
  } catch (error) {
    notify.error(error)
  } finally {
    listing.value = null
  }
}

const packageColumns = computed<QTableColumn<NugetPackage>[]>(() => [
  { name: 'name', label: t('browse.nuget.package'), field: 'name', align: 'left', sortable: true },
  { name: 'latest', label: t('browse.nuget.latest'), field: (r) => r.latest_version ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'versions', label: t('browse.nuget.versions'), field: 'version_count', align: 'right', sortable: true },
  { name: 'updated', label: t('browse.modified'), field: (r) => r.updated_at ?? '', align: 'left', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
])

const columns = computed<QTableColumn<NugetVersion>[]>(() => [
  { name: 'version', label: t('browse.nuget.version'), field: 'version', align: 'left' },
  { name: 'published', label: t('browse.nuget.published'), field: (r) => r.published_at ?? '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size_bytes ?? 0, align: 'right' },
  { name: 'cached', label: t('browse.cache'), field: 'cached', align: 'left' },
  { name: 'actions', label: '', field: () => '', align: 'right' },
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
  max-width: 560px;
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
  font-weight: 650;
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

.ver {
  font-weight: 600;
}

.ver--unlisted {
  color: var(--text-secondary);
}

.badge {
  margin-left: 8px;
  height: 18px;
  font-size: 11px;
}

.by {
  margin-top: 2px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.actions {
  white-space: nowrap;
}
</style>
