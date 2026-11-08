<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Folder listing (raw repositories, and the generic tree for any format): path crumbs, folders first. -->
<template>
  <PageCard flush>
    <template #title><BrowseCrumbs :crumbs="crumbs" @go="goTo" /></template>
    <q-table
      class="ws-table"
      flat
      :rows="listing.data.value?.entries ?? []"
      :columns="columns"
      row-key="path"
      :loading="listing.loading.value"
      :pagination="{ rowsPerPage: 0 }"
      hide-pagination
      :no-data-label="t('browse.emptyFolder')"
      @row-click="(_e: Event, row: FolderEntry) => (row.kind === 'folder' ? (path = row.path) : undefined)"
    >
      <template #body-cell-name="{ row }">
        <q-td :class="{ 'cursor-pointer': row.kind === 'folder' }">
          <span class="entry">
            <q-icon :name="row.kind === 'folder' ? iconFolder : iconFile" size="18px" :class="row.kind === 'folder' ? 'entry__folder' : 'text-faint'" />
            <span :class="{ mono: row.kind === 'file', entry__name: true }">{{ row.name }}</span>
            <span v-if="row.kind === 'folder' && row.child_count" class="text-faint entry__count">{{ row.child_count }}</span>
          </span>
        </q-td>
      </template>
      <template #body-cell-size="{ row }"><q-td class="text-right"><SizeCell v-if="row.kind === 'file'" :bytes="row.size" /></q-td></template>
      <template #body-cell-modified="{ row }"><q-td><TimeCell v-if="row.kind === 'file'" :value="row.last_modified" /></q-td></template>
      <template #body-cell-last="{ row }"><q-td><TimeCell v-if="row.kind === 'file'" :value="row.last_downloaded_at" :placeholder="t('repos.never')" /></q-td></template>
      <template #body-cell-actions="{ row }">
        <q-td class="text-right" @click.stop>
          <q-btn v-if="row.kind === 'file'" flat dense round size="sm" :icon="iconCopy" :aria-label="t('browse.copyUrl')" @click="copy(`${repo.url}${row.path}`, t('browse.url'))">
            <q-tooltip>{{ t('browse.copyUrl') }}</q-tooltip>
          </q-btn>
        </q-td>
      </template>
    </q-table>
  </PageCard>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed } from 'vue'

import { api, call } from '@/api/client'
import type { FolderEntry, Repository } from '@/api/types'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy } from '@/composables/useNotify'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconCopy, iconFile, iconFolder } from '@/utils/icons'

import BrowseCrumbs from './BrowseCrumbs.vue'

const props = defineProps<{ repo: Repository }>()
const path = useQueryParam('path')
const copy = useCopy()

const listing = useAsync(
  () => call(api.GET('/repositories/{name}/browse', { params: { path: { name: props.repo.name }, query: { path: path.value ?? '', limit: 500 } } })),
  [path],
)

const segments = computed(() => (path.value ? path.value.split('/') : []))
const crumbs = computed(() => [{ label: props.repo.name, icon: iconFolder }, ...segments.value.map((s) => ({ label: s }))])

function goTo(index: number): void {
  path.value = index === 0 ? null : segments.value.slice(0, index).join('/')
}

const columns = computed<QTableColumn<FolderEntry>[]>(() => [
  { name: 'name', label: t('browse.name'), field: 'name', align: 'left' },
  { name: 'size', label: t('browse.size'), field: (r) => r.size ?? 0, align: 'right' },
  { name: 'modified', label: t('browse.modified'), field: (r) => r.last_modified ?? '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'downloads', label: t('browse.downloads'), field: (r) => r.download_count ?? null, format: (v: number | null) => (v === null ? '' : formatCount(v)), align: 'right', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'last', label: t('browse.lastDownloaded'), field: (r) => r.last_downloaded_at ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'actions', label: '', field: 'path', align: 'right' },
])
</script>

<style scoped>
.entry {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.entry__folder {
  color: var(--q-primary);
}

.entry__name {
  font-weight: 550;
}

.entry__count {
  font-size: var(--fs-12);
}
</style>
