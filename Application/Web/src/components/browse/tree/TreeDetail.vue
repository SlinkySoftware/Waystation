<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Right pane of the two-pane browse (T-U05): a folder's contents (virtualised, pages load as you scroll) or a
     file's facts with its download URL and digest to copy. -->
<template>
  <div class="tdetail">
    <header class="tdetail__head">
      <q-icon :name="isFolder ? iconFolder : iconFile" size="22px" :class="isFolder ? 'tdetail__folder' : 'text-faint'" />
      <div class="tdetail__titles">
        <h3 class="tdetail__name" :class="{ mono: !isFolder }">{{ name }}</h3>
        <div class="tdetail__path mono">/{{ path }}</div>
      </div>
    </header>

    <template v-if="!isFolder && entry">
      <dl class="tdetail__facts">
        <div><dt>{{ t('browse.size') }}</dt><dd><SizeCell :bytes="entry.size" /></dd></div>
        <div><dt>{{ t('ui.tree.contentType') }}</dt><dd class="mono">{{ entry.content_type || '–' }}</dd></div>
        <div><dt>{{ t('browse.modified') }}</dt><dd><TimeCell :value="entry.last_modified" /></dd></div>
        <div><dt>{{ t('browse.lastDownloaded') }}</dt><dd><TimeCell :value="entry.last_downloaded_at" :placeholder="t('repos.never')" /></dd></div>
        <div><dt>{{ t('browse.downloads') }}</dt><dd>{{ entry.download_count === null || entry.download_count === undefined ? '–' : formatCount(entry.download_count) }}</dd></div>
      </dl>
      <div class="tdetail__copy">
        <div class="section-label">{{ t('browse.url') }}</div>
        <CopyField :value="`${repo.url}${entry.path}`" :label="t('browse.url')" />
      </div>
      <div v-if="entry.sha256" class="tdetail__copy">
        <div class="section-label">SHA-256</div>
        <CopyField :value="entry.sha256" label="SHA-256" />
      </div>
    </template>

    <template v-else-if="isFolder">
      <div class="tdetail__summary">
        {{ folder?.loaded ? t('ui.tree.items', { count: total, n: total.toLocaleString('en') }) : t('common.loading') }}
      </div>
      <q-virtual-scroll
        v-if="folder?.entries.length"
        type="table"
        class="tdetail__table ws-table"
        :items="folder.entries"
        :virtual-scroll-item-size="36"
        :virtual-scroll-sticky-size-start="34"
        @virtual-scroll="onVirtualScroll"
      >
        <template #before>
          <thead>
            <tr>
              <th class="text-left">{{ t('browse.name') }}</th>
              <th class="text-right">{{ t('browse.size') }}</th>
              <th class="text-left gt-xs">{{ t('browse.modified') }}</th>
            </tr>
          </thead>
        </template>
        <template #default="{ item, index }">
          <tr :key="index" class="cursor-pointer" @click="emit('open', item.path)">
            <td>
              <button type="button" class="tdetail__entry" @click.stop="emit('open', item.path)">
                <q-icon :name="item.kind === 'folder' ? iconFolder : iconFile" size="16px" :class="item.kind === 'folder' ? 'tdetail__folder' : 'text-faint'" />
                <span :class="{ mono: item.kind === 'file' }">{{ item.name }}</span>
              </button>
            </td>
            <td class="text-right"><SizeCell v-if="item.kind === 'file'" :bytes="item.size" /></td>
            <td class="gt-xs"><TimeCell v-if="item.kind === 'file'" :value="item.last_modified" /></td>
          </tr>
        </template>
      </q-virtual-scroll>
      <EmptyState v-else-if="folder?.loaded" compact illustration="cache" :title="t('browse.emptyFolder')" />
    </template>

    <EmptyState v-else compact illustration="notFound" :title="t('ui.tree.missing')" :text="t('ui.tree.missingText')" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { FolderEntry, Repository } from '@/api/types'
import CopyField from '@/components/CopyField.vue'
import EmptyState from '@/components/EmptyState.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconFile, iconFolder } from '@/utils/icons'

import type { FolderState } from './folderTree'

const props = defineProps<{
  repo: Repository
  path: string
  /** The selected entry (null for the root, or while it loads). */
  entry: FolderEntry | null
  /** Contents when the selection is a folder. */
  folder: FolderState | null
}>()
const emit = defineEmits<{ open: [path: string]; more: [path: string] }>()

const isFolder = computed(() => props.path === '' || props.entry?.kind === 'folder')
const name = computed(() => (props.path === '' ? props.repo.name : (props.entry?.name ?? props.path.split('/').pop() ?? '')))
const total = computed(() => props.entry?.child_count ?? props.folder?.entries.length ?? 0)

function onVirtualScroll({ to }: { to: number }): void {
  const f = props.folder
  if (f && f.cursor !== null && !f.loading && to >= f.entries.length - 40) emit('more', props.path)
}
</script>

<style scoped>
.tdetail {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
  height: 100%;
}

.tdetail__head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.tdetail__titles {
  min-width: 0;
}

.tdetail__name {
  margin: 0;
  font-size: var(--fs-16);
  font-weight: 650;
  overflow-wrap: anywhere;
}

.tdetail__path {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.tdetail__folder {
  color: var(--q-primary);
}

.tdetail__facts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 12px 20px;
  margin: 0;
}

.tdetail__facts dt {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.tdetail__facts dd {
  margin: 2px 0 0;
  font-weight: 550;
  overflow-wrap: anywhere;
}

.tdetail__copy {
  display: grid;
  gap: 4px;
}

.tdetail__summary {
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.tdetail__table {
  flex: 1;
  min-height: 200px;
  max-height: 560px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.tdetail__table thead tr th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--surface);
  font-size: var(--fs-12);
  font-weight: 600;
  color: var(--text-secondary);
  border-bottom: 1px solid var(--border);
}

.tdetail__table td {
  height: 36px;
  font-size: var(--fs-13);
  border-bottom: 1px solid var(--border);
}

.tdetail__entry {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  cursor: pointer;
  text-align: left;
}

.tdetail__entry:hover {
  color: var(--q-primary);
}
</style>
