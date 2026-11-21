<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Virtualised tree (T-U05, WAI-ARIA tree pattern): only the rows in view (plus a margin) are in the DOM, so a
     folder of 100,000 files scrolls like ten. One tab stop: the tree itself, with `aria-activedescendant` naming
     the active row; arrows move, Right/Left open and close (or go to the first child / the parent), Home/End,
     Page Up/Down, Enter or Space selects. A "more" row at the end of a partly loaded folder loads the next page
     when it scrolls into view. -->
<template>
  <div
    ref="scroller"
    class="vtree"
    role="tree"
    tabindex="0"
    :aria-label="label"
    :aria-activedescendant="activeRowId ?? undefined"
    :aria-busy="busy || undefined"
    @keydown="onKey"
    @scroll.passive="onScroll"
    @wheel.passive="follow = false"
    @pointerdown="follow = false"
    @focus="ensureActive"
  >
    <div class="vtree__space" role="none" :style="{ height: `${rows.length * ROW}px` }">
      <template v-for="(row, i) in windowRows" :key="row.key">
        <div
          v-if="row.type === 'node'"
          :id="rowId(row.key)"
          role="treeitem"
          class="vtree__row"
          :class="{ 'vtree__row--active': row.key === activeKey, 'vtree__row--selected': row.path === selected }"
          :aria-level="row.level"
          :aria-setsize="row.setsize"
          :aria-posinset="row.posinset"
          :aria-expanded="row.kind === 'folder' ? isExpanded(row.path) : undefined"
          :aria-selected="row.path === selected"
          :style="{ transform: `translateY(${(start + i) * ROW}px)`, paddingLeft: `${8 + (row.level - 1) * INDENT}px` }"
          @click="onClick(row)"
        >
          <span class="vtree__twisty" aria-hidden="true" @click.stop="row.kind === 'folder' && toggle(row.path)">
            <q-icon v-if="row.kind === 'folder' && row.path !== ''" :name="iconChevronRight" size="16px" :class="{ 'vtree__twisty--open': isExpanded(row.path) }" />
          </span>
          <q-icon :name="row.path === '' ? iconRepositories : row.kind === 'folder' ? (isExpanded(row.path) ? iconBrowse : iconFolder) : iconFile" size="18px" :class="row.kind === 'folder' ? 'vtree__folder' : 'text-faint'" aria-hidden="true" />
          <span class="vtree__name" :class="{ mono: row.kind === 'file' }">{{ row.name }}</span>
          <span v-if="row.entry?.kind === 'folder' && row.entry.child_count" class="vtree__count">{{ formatCount(row.entry.child_count) }}</span>
        </div>
        <div
          v-else
          :id="rowId(row.key)"
          role="treeitem"
          class="vtree__row vtree__row--more"
          :class="{ 'vtree__row--active': row.key === activeKey }"
          :aria-level="row.level"
          :aria-selected="false"
          :style="{ transform: `translateY(${(start + i) * ROW}px)`, paddingLeft: `${8 + (row.level - 1) * INDENT + 22}px` }"
          @click="emit('more', row.path)"
        >
          <q-spinner v-if="row.loading" size="14px" />
          <span>{{ row.failed ? t('ui.tree.loadFailed') : row.loading ? t('common.loading') : t('ui.tree.more') }}</span>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconBrowse, iconChevronRight, iconFile, iconFolder, iconRepositories } from '@/utils/icons'

import type { TreeRow } from './folderTree'

const props = defineProps<{
  rows: TreeRow[]
  /** Path of the selected node ('' = the repository root). */
  selected: string | null
  label: string
  isExpanded: (path: string) => boolean
  busy?: boolean
}>()
const emit = defineEmits<{ select: [path: string]; toggle: [path: string, open: boolean]; more: [path: string] }>()

const ROW = 32
const INDENT = 18
const OVERSCAN = 12
const uid = Math.random().toString(36).slice(2, 8)

const scroller = ref<HTMLDivElement | null>(null)
const scrollTop = ref(0)
const viewport = ref(480)
const activeKey = ref<string | null>(null)

const start = computed(() => Math.max(0, Math.floor(scrollTop.value / ROW) - OVERSCAN))
const end = computed(() => Math.min(props.rows.length, Math.ceil((scrollTop.value + viewport.value) / ROW) + OVERSCAN))
const windowRows = computed(() => props.rows.slice(start.value, end.value))
const activeIndex = computed(() => (activeKey.value === null ? -1 : props.rows.findIndex((r) => r.key === activeKey.value)))
const activeRowId = computed(() => (activeIndex.value >= 0 ? rowId(activeKey.value!) : null))

function rowId(key: string): string {
  // Ids must not contain spaces; keys are paths, so encode them.
  return `vt-${uid}-${encodeURIComponent(key).replace(/%/g, '_')}`
}

let raf = 0
function onScroll(): void {
  if (raf) return
  raf = requestAnimationFrame(() => {
    raf = 0
    scrollTop.value = scroller.value?.scrollTop ?? 0
  })
}

let observer: ResizeObserver | null = null
onMounted(() => {
  if (!scroller.value) return
  viewport.value = scroller.value.clientHeight || 480
  observer = new ResizeObserver(() => (viewport.value = scroller.value?.clientHeight || viewport.value))
  observer.observe(scroller.value)
})
onBeforeUnmount(() => {
  observer?.disconnect()
  if (raf) cancelAnimationFrame(raf)
})

// "More" rows in view ask for the next page.
watch(
  windowRows,
  (visible) => {
    for (const row of visible) if (row.type === 'more' && !row.loading && !row.failed) emit('more', row.path)
  },
  { immediate: true },
)

// Follow the selection (deep links, clicks in the detail pane): once per change of the selected path, as soon as
// its row exists (its folders may still be loading).
let pending = true
watch(
  () => props.selected,
  () => (pending = true),
)
watch(
  () => [props.selected, props.rows.length] as const,
  ([path]) => {
    if (path === null || !pending) return
    const index = props.rows.findIndex((r) => r.type === 'node' && r.path === path)
    if (index < 0) return
    pending = false
    if (activeKey.value !== props.rows[index]!.key) {
      activeKey.value = props.rows[index]!.key
      scrollIntoView(index)
    }
  },
  { immediate: true },
)

function scrollIntoView(index: number): void {
  void nextTick(() => {
    const el = scroller.value
    if (!el) return
    const top = index * ROW
    if (top < el.scrollTop) el.scrollTop = top
    else if (top + ROW > el.scrollTop + el.clientHeight) el.scrollTop = top + ROW - el.clientHeight
    scrollTop.value = el.scrollTop
  })
}

function ensureActive(): void {
  if (activeIndex.value < 0 && props.rows.length) activeKey.value = props.rows[0]!.key
}

/** After a keyboard move, keep the active row in view while folders above it load more rows (until the user
 * scrolls or clicks). */
let follow = false
function moveTo(index: number): void {
  const i = Math.max(0, Math.min(props.rows.length - 1, index))
  const row = props.rows[i]
  if (!row) return
  activeKey.value = row.key
  follow = true
  scrollIntoView(i)
}
watch(
  () => props.rows,
  () => {
    if (follow && activeIndex.value >= 0) scrollIntoView(activeIndex.value)
  },
)

function toggle(path: string): void {
  emit('toggle', path, !props.isExpanded(path))
}

function onClick(row: TreeRow): void {
  activeKey.value = row.key
  if (row.type !== 'node') return
  emit('select', row.path)
  if (row.kind === 'folder' && row.path !== '' && !props.isExpanded(row.path)) emit('toggle', row.path, true)
}

function parentIndex(index: number): number {
  const level = props.rows[index]?.level ?? 1
  for (let i = index - 1; i >= 0; i--) if (props.rows[i]!.level < level) return i
  return -1
}

function onKey(event: KeyboardEvent): void {
  ensureActive()
  const i = activeIndex.value
  const row = props.rows[i]
  if (!row) return
  const page = Math.max(1, Math.floor(viewport.value / ROW) - 1)
  let handled = true
  switch (event.key) {
    case 'ArrowDown':
      moveTo(i + 1)
      break
    case 'ArrowUp':
      moveTo(i - 1)
      break
    case 'PageDown':
      moveTo(i + page)
      break
    case 'PageUp':
      moveTo(i - page)
      break
    case 'Home':
      moveTo(0)
      break
    case 'End':
      moveTo(props.rows.length - 1)
      break
    case 'ArrowRight':
      if (row.type === 'node' && row.kind === 'folder') {
        if (!props.isExpanded(row.path)) emit('toggle', row.path, true)
        else if ((props.rows[i + 1]?.level ?? 0) > row.level) moveTo(i + 1)
      }
      break
    case 'ArrowLeft':
      if (row.type === 'node' && row.kind === 'folder' && row.path !== '' && props.isExpanded(row.path)) emit('toggle', row.path, false)
      else if (parentIndex(i) >= 0) moveTo(parentIndex(i))
      break
    case 'Enter':
    case ' ':
      if (row.type === 'node') emit('select', row.path)
      else emit('more', row.path)
      break
    default:
      handled = false
  }
  if (handled) event.preventDefault()
}
</script>

<style scoped>
.vtree {
  position: relative;
  height: 100%;
  min-height: 240px;
  overflow: auto;
  outline: none;
  contain: strict;
}

.vtree:focus-visible {
  box-shadow: inset 0 0 0 2px var(--q-primary);
  border-radius: var(--radius-sm);
}

.vtree__space {
  position: relative;
  min-width: 100%;
}

.vtree__row {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 32px;
  display: flex;
  align-items: center;
  gap: 6px;
  padding-right: 12px;
  font-size: var(--fs-13);
  white-space: nowrap;
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.vtree__row:hover {
  background: var(--hover-bg);
}

.vtree__row--selected {
  background: var(--selected-bg);
  font-weight: 600;
}

.vtree:focus-visible .vtree__row--active {
  box-shadow: inset 0 0 0 2px var(--q-primary);
}

.vtree__row--more {
  color: var(--text-secondary);
  font-style: italic;
}

.vtree__twisty {
  display: inline-flex;
  width: 16px;
  flex: none;
  justify-content: center;
}

.vtree__twisty .q-icon {
  color: var(--text-tertiary);
  transition: transform var(--dur) var(--ease-out);
}

.vtree__twisty--open {
  transform: rotate(90deg);
}

.vtree__folder {
  color: var(--q-primary);
}

.vtree__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.vtree__count {
  margin-left: auto;
  padding-left: 8px;
  font-size: var(--fs-12);
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
</style>
