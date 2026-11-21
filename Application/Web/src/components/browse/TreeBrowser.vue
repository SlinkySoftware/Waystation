<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Two-pane browse (UI_DESIGN §4.4, T-U05): a virtualised, lazily loaded folder tree on the left and the selected
     folder or file on the right. Works for every format (the generic asset listing); the selection is in the URL
     (`?path=`), so a link opens the tree at that file. -->
<template>
  <section class="tree-browser q-card" :aria-label="t('ui.tree.label', { name: repo.name })">
    <div class="tree-browser__tree">
      <VirtualTree :rows="tree.rows.value" :selected="selected" :label="t('ui.tree.label', { name: repo.name })" :is-expanded="tree.isExpanded" @select="select" @toggle="onToggle" @more="(p) => void tree.load(p, true)" />
    </div>
    <div class="tree-browser__detail">
      <LoadFailedState v-if="rootError" :error="rootError" :title="t('ui.tree.loadFailed')" compact />
      <TreeDetail v-else :repo="repo" :path="selected" :entry="entry" :folder="folderView" @open="open" @more="(p) => void tree.load(p, true)" />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, toRef, watch } from 'vue'

import { api, call } from '@/api/client'
import type { FolderEntry, Repository } from '@/api/types'
import LoadFailedState from '@/components/LoadFailedState.vue'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'

import { PAGE, useFolderTree } from './tree/folderTree'
import TreeDetail from './tree/TreeDetail.vue'
import VirtualTree from './tree/VirtualTree.vue'

const props = defineProps<{ repo: Repository }>()
const pathParam = useQueryParam('path')
const selected = computed(() => pathParam.value ?? '')

const tree = useFolderTree(
  (path, cursor) =>
    call(api.GET('/repositories/{name}/browse', { params: { path: { name: props.repo.name }, query: { path, limit: PAGE, ...(cursor ? { cursor } : {}) } } })),
  toRef(() => props.repo.name),
)

const entry = ref<FolderEntry | null>(null)
const rootError = computed(() => {
  void tree.version.value
  return tree.state('').error
})
/** A fresh copy on every tree change: the tree's own state is not reactive (it may hold 100,000 entries). */
const folderView = computed(() => {
  void tree.version.value
  const isFolder = selected.value === '' || entry.value?.kind === 'folder'
  return isFolder ? { ...tree.state(selected.value) } : null
})

// Open the folders above the selection, find its entry and load a selected folder's contents.
watch(
  [selected, () => props.repo.name],
  async ([path], old) => {
    if (old && old[1] !== props.repo.name) tree.reset()
    const found = await tree.reveal(path)
    if (path !== selected.value) return
    entry.value = found
    if (path === '' || found?.kind === 'folder') await tree.expand(path)
  },
  { immediate: true },
)

function select(path: string): void {
  pathParam.value = path || null
}

function open(path: string): void {
  select(path)
}

function onToggle(path: string, isOpen: boolean): void {
  if (isOpen) void tree.expand(path)
  else tree.collapse(path)
}
</script>

<style scoped>
.tree-browser {
  display: grid;
  grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
  height: clamp(420px, calc(100vh - 330px), 820px);
  overflow: hidden;
}

.tree-browser__tree {
  min-width: 0;
  padding: 8px;
  border-right: 1px solid var(--border);
  background: var(--surface-subtle);
}

.tree-browser__detail {
  min-width: 0;
  padding: 16px 20px;
  overflow: auto;
}

@media (max-width: 1023px) {
  .tree-browser {
    grid-template-columns: 1fr;
    grid-template-rows: 320px auto;
    height: auto;
  }

  .tree-browser__tree {
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
}
</style>
