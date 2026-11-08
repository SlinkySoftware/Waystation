<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Docker browse: images → tags (digest, platforms, size, created, last pulled, pulls) → manifest. -->
<template>
  <PageCard flush>
    <template #title>
      <BrowseCrumbs :crumbs="crumbs" @go="go" />
    </template>
    <template v-if="!image" #actions>
      <q-input v-model="search" dense outlined clearable debounce="200" :placeholder="t('browse.docker.searchImages')" class="browse-search">
        <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
      </q-input>
    </template>

    <DockerManifestView v-if="image && ref" :repo="repo" :image="image" :reference="ref" />

    <q-table
      v-else-if="image"
      class="ws-table"
      flat
      :rows="tags.data.value?.items ?? []"
      :columns="tagColumns"
      row-key="tag"
      :loading="tags.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :grid="$q.screen.lt.sm"
      @row-click="(_e: Event, row: DockerTag) => nav({ ref: row.tag, platform: null })"
    >
      <template #body-cell-tag="{ row }">
        <q-td class="cursor-pointer"><span class="tag-name mono">{{ row.tag }}</span></q-td>
      </template>
      <template #body-cell-digest="{ row }">
        <q-td @click.stop><CopyField :value="shortDigest(row.digest)" compact :label="t('browse.docker.digest')" /></q-td>
      </template>
      <template #body-cell-platforms="{ row }">
        <q-td><PlatformBadges :platforms="row.platforms" :max="4" /></q-td>
      </template>
      <template #body-cell-size="{ row }">
        <q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td>
      </template>
      <template #body-cell-created="{ row }">
        <q-td><TimeCell :value="row.created_at" /></q-td>
      </template>
      <template #body-cell-pulled="{ row }">
        <q-td><TimeCell :value="row.last_pulled_at" :placeholder="t('repos.never')" /></q-td>
      </template>
      <template #item="{ row }">
        <button type="button" class="m-card" @click="nav({ ref: row.tag, platform: null })">
          <span class="mono tag-name">{{ row.tag }}</span>
          <span class="text-caption">{{ formatBytes(row.size_bytes) }} · {{ t('browse.docker.platformCount', { count: row.platforms.length }) }}</span>
        </button>
      </template>
    </q-table>

    <q-table
      v-else
      class="ws-table"
      flat
      :rows="images.data.value?.items ?? []"
      :columns="imageColumns"
      row-key="name"
      :loading="images.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :grid="$q.screen.lt.sm"
      :no-data-label="t('browse.empty')"
      @row-click="(_e: Event, row: DockerImage) => nav({ image: row.name })"
    >
      <template #body-cell-name="{ row }">
        <q-td class="cursor-pointer">
          <span class="img-name"><q-icon :name="iconLayers" size="18px" class="text-faint" /><span class="mono">{{ row.name }}</span></span>
        </q-td>
      </template>
      <template #body-cell-size="{ row }">
        <q-td class="text-right"><SizeCell :bytes="row.size_bytes" /></q-td>
      </template>
      <template #body-cell-pulled="{ row }">
        <q-td><TimeCell :value="row.last_pulled_at" :placeholder="t('repos.never')" /></q-td>
      </template>
      <template #item="{ row }">
        <button type="button" class="m-card" @click="nav({ image: row.name })">
          <span class="mono">{{ row.name }}</span>
          <span class="text-caption">{{ t('browse.docker.tagCount', { count: row.tag_count }) }} · {{ formatBytes(row.size_bytes) }}</span>
        </button>
      </template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref as vueRef } from 'vue'

import { api, call } from '@/api/client'
import type { DockerImage, DockerTag, Repository } from '@/api/types'
import CopyField from '@/components/CopyField.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useQueryNavigate, useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { formatBytes, formatCount, shortDigest } from '@/utils/format'
import { iconLayers, iconSearch } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'
import DockerManifestView from './DockerManifestView.vue'
import PlatformBadges from './PlatformBadges.vue'

const props = defineProps<{ repo: Repository }>()

const image = useQueryParam('image')
const ref = useQueryParam('ref')
const nav = useQueryNavigate()
const search = vueRef<string | null>(null)

const images = useAsync(
  () => call(api.GET('/repositories/{name}/docker/images', { params: { path: { name: props.repo.name }, query: { q: search.value || undefined, limit: 200 } } })),
  [search],
)
const tags = useAsync(
  async () => (image.value ? call(api.GET('/repositories/{name}/docker/tags', { params: { path: { name: props.repo.name }, query: { image: image.value, limit: 200 } } })) : null),
  [image],
)

const crumbs = computed(() => [
  { label: t('browse.docker.images') },
  ...(image.value ? [{ label: image.value, mono: true }] : []),
  ...(image.value && ref.value ? [{ label: ref.value, mono: true }] : []),
])

function go(index: number): void {
  if (index === 0) nav({ image: null, ref: null, platform: null })
  else nav({ ref: null, platform: null })
}

const imageColumns = computed<QTableColumn<DockerImage>[]>(() => [
  { name: 'name', label: t('browse.docker.image'), field: 'name', align: 'left', sortable: true },
  { name: 'tags', label: t('browse.docker.tags'), field: 'tag_count', align: 'right', sortable: true },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right', sortable: true },
  { name: 'pulls', label: t('browse.docker.pulls'), field: (r) => r.pulls ?? 0, format: (v: number) => formatCount(v), align: 'right', sortable: true },
  { name: 'pulled', label: t('browse.docker.lastPulled'), field: (r) => r.last_pulled_at ?? '', align: 'left', sortable: true },
])

const tagColumns = computed<QTableColumn<DockerTag>[]>(() => [
  { name: 'tag', label: t('browse.docker.tag'), field: 'tag', align: 'left', sortable: true },
  { name: 'digest', label: t('browse.docker.digest'), field: 'digest', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'platforms', label: t('browse.docker.platforms'), field: (r) => r.platforms.length, align: 'left' },
  { name: 'size', label: t('browse.size'), field: 'size_bytes', align: 'right', sortable: true },
  { name: 'created', label: t('browse.docker.created'), field: (r) => r.created_at ?? '', align: 'left', sortable: true, classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'pulled', label: t('browse.docker.lastPulled'), field: (r) => r.last_pulled_at ?? '', align: 'left', sortable: true },
  { name: 'pulls', label: t('browse.docker.pulls'), field: (r) => r.pulls ?? 0, format: (v: number) => formatCount(v), align: 'right', sortable: true },
])
</script>

<style scoped>
.browse-search {
  width: 260px;
}

.img-name {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-weight: 550;
}

.tag-name {
  font-weight: 600;
}

.m-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: calc(100% - 24px);
  margin: 0 12px 8px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-primary);
  font: inherit;
  text-align: left;
}
</style>
