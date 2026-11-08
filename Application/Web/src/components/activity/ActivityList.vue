<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- The activity log: a virtualised list, newest first. Reports when its newest rows leave the view (the page then holds
     new events instead of shifting the list) and when the oldest loaded rows come into view (load more history). -->
<template>
  <div class="act-list" :class="`act-list--${layout}`">
    <div v-if="!compact" class="act-list__head act-row" aria-hidden="true">
      <span>{{ t('activity.col.time') }}</span>
      <span>{{ t('activity.col.event') }}</span>
      <span>{{ t('activity.col.repository') }}</span>
      <span class="act-row__who">{{ t('activity.col.who') }}</span>
      <span>{{ t('activity.col.cache') }}</span>
      <span class="act-row__num">{{ t('activity.col.size') }}</span>
      <span class="act-row__num act-row__dur">{{ t('activity.col.duration') }}</span>
    </div>
    <q-virtual-scroll
      ref="scroller"
      class="act-list__scroll"
      :items="items"
      :virtual-scroll-item-size="compact ? 62 : 44"
      :virtual-scroll-slice-size="40"
      role="list"
      :aria-label="t('nav.activity')"
      @virtual-scroll="onVirtualScroll"
    >
      <template #default="{ item, index }">
        <div
          :key="item.id"
          class="act-row act-row--item"
          :class="{ 'act-row--failure': item.outcome === 'failure', 'act-row--selected': item.id === selectedId, 'act-row--fresh': freshIds.has(item.id) }"
          role="listitem"
          tabindex="0"
          :aria-label="`${absoluteTime(item.ts)}: ${item.summary}`"
          :data-index="index"
          @click="emit('select', item)"
          @keydown.enter.prevent="emit('select', item)"
          @keydown.down.prevent="focusSibling($event, 1)"
          @keydown.up.prevent="focusSibling($event, -1)"
        >
          <span class="act-row__time">
            <time :datetime="item.ts">{{ clock(item.ts) }}</time>
            <q-tooltip>{{ absoluteTime(item.ts) }}</q-tooltip>
          </span>
          <span class="act-row__event">
            <span class="act-row__icon" :class="activityTone(item)"><q-icon :name="kindMeta(item.kind).icon" size="15px" /></span>
            <span class="act-row__summary">{{ item.summary }}</span>
          </span>
          <span class="act-row__repo">
            <RepoChip v-if="item.repository" :name="item.repository" :format="item.format ?? null" :link="false" />
          </span>
          <span class="act-row__who ellipsis">{{ item.principal ?? '' }}</span>
          <span class="act-row__cache"><CacheBadge v-if="item.cache" :state="item.cache" /></span>
          <span class="act-row__num">{{ item.bytes !== null && item.bytes !== undefined ? formatBytes(item.bytes) : '' }}</span>
          <span class="act-row__num act-row__dur">{{ item.duration_ms !== null && item.duration_ms !== undefined ? formatDuration(item.duration_ms / 1000) : '' }}</span>
        </div>
      </template>
      <template #after>
        <div class="act-list__foot">
          <q-spinner v-if="loading" size="18px" class="q-mr-sm" />
          <span v-if="loading">{{ t('activity.loadingMore') }}</span>
          <span v-else-if="done && items.length">{{ t('activity.endOfHistory') }}</span>
          <q-btn v-else-if="!done && items.length" flat dense no-caps :label="t('common.loadMore')" @click="emit('more')" />
        </div>
      </template>
    </q-virtual-scroll>
  </div>
</template>

<script setup lang="ts">
import type { QVirtualScroll } from 'quasar'
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import type { ActivityEvent } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import RepoChip from '@/components/RepoChip.vue'
import { t } from '@/i18n'
import { activityTone, kindMeta } from '@/utils/activity'
import { absoluteTime, formatBytes, formatDuration } from '@/utils/format'

const props = withDefaults(
  defineProps<{ items: ActivityEvent[]; loading?: boolean; done?: boolean; selectedId?: string | null; layout?: 'full' | 'medium' | 'compact' }>(),
  { loading: false, done: false, selectedId: null, layout: 'full' },
)
const emit = defineEmits<{ select: [ActivityEvent]; more: []; 'at-top': [boolean] }>()
const compact = computed(() => props.layout === 'compact')

const scroller = ref<QVirtualScroll | null>(null)
const LOAD_AHEAD = 30
const TOP_PX = 8

function onVirtualScroll(details: { to: number }): void {
  if (details.to >= props.items.length - LOAD_AHEAD && !props.loading && !props.done) emit('more')
}

let element: HTMLElement | null = null
let atTop = true
function onScroll(): void {
  const next = (element?.scrollTop ?? 0) <= TOP_PX
  if (next !== atTop) {
    atTop = next
    emit('at-top', next)
  }
}
onMounted(() => {
  element = (scroller.value?.$el as HTMLElement | undefined) ?? null
  element?.addEventListener('scroll', onScroll, { passive: true })
})
onBeforeUnmount(() => element?.removeEventListener('scroll', onScroll))

/** Scroll to the newest row (after resuming). */
function scrollToTop(): void {
  element?.scrollTo({ top: 0 })
}

// Rows that arrived live get a short highlight.
const freshIds = shallowRef(new Set<string>())
let known = new Set<string>()
let freshTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => props.items,
  (items, previous) => {
    const next = new Set(items.map((e) => e.id))
    if (previous?.length && items.length && items[0]!.id !== previous[0]?.id) {
      const fresh = items.slice(0, 50).filter((e) => !known.has(e.id))
      if (fresh.length && fresh.length < items.length) {
        freshIds.value = new Set(fresh.map((e) => e.id))
        if (freshTimer) clearTimeout(freshTimer)
        freshTimer = setTimeout(() => (freshIds.value = new Set()), 1600)
      }
    }
    known = next
  },
)
onBeforeUnmount(() => {
  if (freshTimer) clearTimeout(freshTimer)
})

const sameDay = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
const dayFmt = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' })
/** "14:03:12" today, "8 Nov 14:03:12" before. */
function clock(iso: string): string {
  const d = new Date(iso)
  return sameDay(d, new Date()) ? timeFmt.format(d) : `${dayFmt.format(d)} ${timeFmt.format(d)}`
}

function focusSibling(event: KeyboardEvent, step: number): void {
  const row = event.currentTarget as HTMLElement
  const target = step > 0 ? row.nextElementSibling : row.previousElementSibling
  if (target instanceof HTMLElement && target.classList.contains('act-row--item')) target.focus()
}

defineExpose({ scrollToTop })
</script>

<style scoped>
.act-list {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.act-list__scroll {
  height: calc(100vh - 330px);
  min-height: 320px;
}

.act-row {
  display: grid;
  grid-template-columns: 128px minmax(220px, 1fr) 190px 150px 118px 82px 76px;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  min-width: 0;
}

.act-list__head {
  height: 34px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-sunken);
  font-size: var(--fs-12);
  font-weight: 600;
  color: var(--text-secondary);
}

.act-row--item {
  height: 44px;
  border-bottom: 1px solid var(--border);
  font-size: var(--fs-13);
  cursor: pointer;
  transition: background-color 600ms var(--ease-out);
}

.act-row--item:hover {
  background: var(--hover-bg);
}

.act-row--item:focus-visible {
  outline: 2px solid var(--q-primary);
  outline-offset: -2px;
}

.act-row--selected {
  background: var(--selected-bg);
}

.act-row--fresh {
  background: var(--surface-brand);
}

.act-row--failure .act-row__summary {
  color: var(--cache-error);
}

.act-row > * {
  min-width: 0;
}

.act-row__time {
  font-variant-numeric: tabular-nums;
  color: var(--text-secondary);
  white-space: nowrap;
}

.act-row__event {
  display: flex;
  align-items: center;
  gap: 10px;
}

.act-row__icon {
  display: grid;
  place-items: center;
  flex: none;
  width: 24px;
  height: 24px;
  border-radius: 7px;
}

.act-row__summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.act-row__repo :deep(.repo-chip) {
  max-width: 100%;
}

.act-row__who {
  color: var(--text-secondary);
}

.act-row__num {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.act-row__dur {
  color: var(--text-secondary);
}

.act-list__foot {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 48px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

/* Mid widths: drop who and duration (both are in the detail drawer). */
.act-list--medium .act-row {
  grid-template-columns: 118px minmax(200px, 1fr) 170px 118px 82px;
}

.act-list--medium .act-row__who,
.act-list--medium .act-row__dur {
  display: none;
}

/* Phones and narrow tablets: two-line rows. */
.act-list--compact .act-row--item {
  grid-template-columns: auto 1fr auto;
  grid-template-areas:
    'icon summary summary'
    'icon meta time';
  height: 62px;
  gap: 2px 10px;
  padding: 8px 12px;
}

.act-list--compact .act-row__event {
  display: contents;
}

.act-list--compact .act-row__icon {
  grid-area: icon;
  align-self: center;
}

.act-list--compact .act-row__summary {
  grid-area: summary;
}

.act-list--compact .act-row__repo {
  grid-area: meta;
}

.act-list--compact .act-row__time {
  grid-area: time;
  font-size: var(--fs-12);
}

.act-list--compact .act-row__who,
.act-list--compact .act-row__cache,
.act-list--compact .act-row__num {
  display: none;
}

@media (prefers-reduced-motion: reduce) {
  .act-row--item {
    transition: none;
  }
}
</style>
