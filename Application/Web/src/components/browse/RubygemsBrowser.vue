<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- RubyGems browse: gems → gem page (gem install and Gemfile snippets, versions newest first as the server orders
     them, one row per platform build, with platform and prerelease badges, the required Ruby, dependencies, cache
     state, size, SHA-256, publish date and download link; hosted: who pushed it). Groups list their members' gems. -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="gem = null" /></template>
    <template v-if="!gem" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.rubygems.search')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <template v-if="gem">
      <div v-if="versions.data.value" class="pkg">
        <div class="pkg__head">
          <div class="col">
            <div class="pkg__name mono">{{ versions.data.value.gem.name }}</div>
            <div v-if="versions.data.value.gem.summary" class="pkg__desc">{{ versions.data.value.gem.summary }}</div>
            <div class="pkg__meta">
              <span v-if="versions.data.value.gem.latest_version">{{ t('browse.rubygems.latest') }} <strong class="mono">{{ versions.data.value.gem.latest_version }}</strong></span>
              <span>{{ t('browse.rubygems.versionCount', { count: versions.data.value.gem.version_count }) }}</span>
            </div>
          </div>
          <div class="pkg__install">
            <div class="section-label">{{ t('browse.rubygems.install') }}</div>
            <CopyField :value="`gem install ${versions.data.value.gem.name}`" />
            <div class="section-label">{{ t('browse.rubygems.gemfile') }}</div>
            <CopyField :value="gemfileLine(versions.data.value.gem)" />
          </div>
        </div>

        <q-table class="ws-table" flat :rows="versions.data.value.items" :columns="columns" :row-key="rowKey" :pagination="{ rowsPerPage: 0 }" hide-pagination :no-data-label="t('browse.empty')">
          <template #body-cell-version="{ row }">
            <q-td>
              <span class="mono ver">{{ row.version }}</span>
              <span v-if="row.platform !== 'ruby'" class="ws-badge platform badge mono">
                {{ row.platform }}
                <q-tooltip max-width="320px">{{ t('browse.rubygems.platformHelp') }}</q-tooltip>
              </span>
              <span v-if="row.prerelease" class="ws-badge tone-offline badge">{{ t('browse.rubygems.prerelease') }}</span>
              <div v-if="row.uploaded_by" class="by">{{ t('browse.rubygems.uploadedBy', { user: row.uploaded_by }) }}</div>
            </q-td>
          </template>
          <template #body-cell-ruby="{ row }">
            <q-td><span v-if="row.ruby_requirement" class="mono req">{{ row.ruby_requirement }}</span></q-td>
          </template>
          <template #body-cell-dependencies="{ row }">
            <q-td>
              <span v-if="row.dependencies.length" class="deps">
                {{ t('browse.rubygems.dependencyCount', { count: row.dependencies.length }) }}
                <q-tooltip max-width="420px">
                  <div v-for="d in row.dependencies" :key="d.name" class="mono">{{ d.name }} {{ d.requirement }}</div>
                </q-tooltip>
              </span>
              <span v-else class="text-faint">{{ t('browse.rubygems.noDependencies') }}</span>
            </q-td>
          </template>
          <template #body-cell-published="{ row }"><q-td><TimeCell :value="row.published_at" placeholder="" /></q-td></template>
          <template #body-cell-size="{ row }">
            <q-td class="text-right"><SizeCell v-if="row.size_bytes !== null" :bytes="row.size_bytes" /></q-td>
          </template>
          <template #body-cell-sha="{ row }">
            <q-td>
              <template v-if="row.sha256">
                <span class="mono digest">{{ row.sha256.slice(0, 12) }}<q-tooltip>sha256:{{ row.sha256 }}</q-tooltip></span>
                <q-btn flat dense round size="xs" :icon="iconCopy" :aria-label="t('browse.rubygems.copyChecksum')" @click="copy(row.sha256, t('browse.rubygems.checksumLabel'))">
                  <q-tooltip>{{ t('browse.rubygems.copyChecksum') }}</q-tooltip>
                </q-btn>
              </template>
            </q-td>
          </template>
          <template #body-cell-cached="{ row }">
            <q-td><CacheBadge v-if="row.cached" state="HIT" /><span v-else class="text-faint">{{ t('browse.notCached') }}</span></q-td>
          </template>
          <template #body-cell-actions="{ row }">
            <q-td auto-width class="actions">
              <template v-if="row.cached">
                <q-btn flat dense round size="sm" :icon="iconCopy" :aria-label="t('browse.copyUrl')" @click="copy(row.download_url, t('browse.url'))">
                  <q-tooltip>{{ t('browse.copyUrl') }}</q-tooltip>
                </q-btn>
                <q-btn flat dense round size="sm" type="a" :href="row.download_url" :icon="iconDownload" :aria-label="t('browse.rubygems.download')">
                  <q-tooltip>{{ t('browse.rubygems.download') }}</q-tooltip>
                </q-btn>
              </template>
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
      :rows="gems.data.value?.items ?? []"
      :columns="gemColumns"
      row-key="name"
      :loading="gems.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: RubygemsGem) => (gem = row.name)"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <div class="mono pkg-name">{{ row.name }}</div>
        </q-td>
      </template>
      <template #body-cell-summary="{ row }">
        <q-td class="cursor-pointer"><div class="pkg-desc">{{ row.summary ?? '' }}</div></q-td>
      </template>
      <template #body-cell-updated="{ row }"><q-td><TimeCell :value="row.updated_at" placeholder="" /></q-td></template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { Repository, RubygemsGem, RubygemsVersion } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy } from '@/composables/useNotify'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { iconCopy, iconDownload, iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const copy = useCopy()
const gem = useQueryParam('gem')
const search = ref<string | null>(null)

const gems = useAsync(
  () => call(api.GET('/repositories/{name}/rubygems/gems', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const versions = useAsync(
  async () => (gem.value ? call(api.GET('/repositories/{name}/rubygems/versions', { params: { path: { name: props.repo.name }, query: { gem: gem.value, limit: 200 } } })) : null),
  [gem],
)

const crumbs = computed(() => [{ label: t('browse.rubygems.gems') }, ...(gem.value ? [{ label: versions.data.value?.gem.name ?? gem.value, mono: true }] : [])])

/** The Gemfile line: a pessimistic constraint on the latest release, or the bare name when there is none. */
const gemfileLine = (g: RubygemsGem): string => (g.latest_version ? `gem '${g.name}', '~> ${g.latest_version}'` : `gem '${g.name}'`)
const rowKey = (r: RubygemsVersion): string => `${r.version}-${r.platform}`

const gemColumns = computed<QTableColumn<RubygemsGem>[]>(() => [
  { name: 'name', label: t('browse.rubygems.gem'), field: 'name', align: 'left', sortable: true },
  { name: 'summary', label: t('browse.rubygems.summary'), field: (r) => r.summary ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'latest', label: t('browse.rubygems.latest'), field: (r) => r.latest_version ?? '', align: 'left', classes: 'mono gt-xs', headerClasses: 'gt-xs' },
  { name: 'versions', label: t('browse.rubygems.versions'), field: 'version_count', align: 'right', sortable: true },
  { name: 'updated', label: t('browse.modified'), field: (r) => r.updated_at ?? '', align: 'left', sortable: true, classes: 'gt-sm', headerClasses: 'gt-sm' },
])

const columns = computed<QTableColumn<RubygemsVersion>[]>(() => [
  { name: 'version', label: t('browse.rubygems.version'), field: 'version', align: 'left' },
  { name: 'ruby', label: t('browse.rubygems.ruby'), field: (r) => r.ruby_requirement ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'dependencies', label: t('browse.rubygems.dependencies'), field: (r) => r.dependencies.length, align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'published', label: t('browse.rubygems.published'), field: (r) => r.published_at ?? '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size_bytes ?? 0, align: 'right' },
  { name: 'sha', label: t('browse.rubygems.checksum'), field: (r) => r.sha256 ?? '', align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
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
  font-size: var(--fs-13);
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
  min-width: 320px;
}

.ver {
  font-weight: 600;
}

.req,
.digest {
  color: var(--text-secondary);
}

.platform {
  color: var(--text-secondary);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 1px var(--border);
}

.deps {
  cursor: default;
  border-bottom: 1px dotted var(--text-secondary);
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
