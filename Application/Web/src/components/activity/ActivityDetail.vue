<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- One activity event in a side panel: every field, copyable path and id, and one-click filters that narrow the log. -->
<template>
  <q-dialog :model-value="event !== null" position="right" full-height @update:model-value="(open) => !open && emit('close')">
    <q-card v-if="event" class="act-detail" role="dialog" :aria-label="t('activity.detail.title')">
      <header class="act-detail__head">
        <span class="act-detail__icon" :class="activityTone(event)"><q-icon :name="meta.icon" size="20px" /></span>
        <div class="act-detail__titles">
          <div class="act-detail__kind">{{ meta.label }}</div>
          <div class="text-caption"><TimeCell :value="event.ts" /></div>
        </div>
        <q-btn flat round dense :icon="iconExpand" class="act-detail__nav-up" :disable="!hasPrevious" :aria-label="t('activity.detail.previous')" @click="emit('step', -1)" />
        <q-btn flat round dense :icon="iconExpand" :disable="!hasNext" :aria-label="t('activity.detail.next')" @click="emit('step', 1)" />
        <q-btn v-close-popup flat round dense :icon="iconClose" :aria-label="t('common.close')" />
      </header>

      <div class="act-detail__body">
        <p class="act-detail__summary" :class="{ 'act-detail__summary--failure': event.outcome === 'failure' }">{{ event.summary }}</p>

        <dl class="act-detail__fields">
          <dt>{{ t('activity.detail.outcome') }}</dt>
          <dd><span class="ws-badge" :class="outcomeTone">{{ t(`activity.outcome.${event.outcome}`) }}</span></dd>
          <dt>{{ t('activity.detail.time') }}</dt>
          <dd class="num">{{ absoluteTime(event.ts) }}</dd>
          <template v-if="event.repository">
            <dt>{{ t('activity.detail.repository') }}</dt>
            <dd><RepoChip :name="event.repository" :format="event.format ?? null" /></dd>
          </template>
          <template v-if="event.format">
            <dt>{{ t('activity.detail.format') }}</dt>
            <dd>{{ formatLabel(event.format) }}</dd>
          </template>
          <template v-if="event.artifact">
            <dt>{{ t('activity.detail.artifact') }}</dt>
            <dd class="mono act-detail__wrap">{{ event.artifact }}</dd>
          </template>
          <template v-if="event.path && event.path !== event.artifact">
            <dt>{{ t('activity.detail.path') }}</dt>
            <dd class="act-detail__copy">
              <span class="mono act-detail__wrap">{{ event.path }}</span>
              <q-btn flat dense round size="sm" :icon="iconCopy" :aria-label="t('common.copyWhat', { what: t('activity.detail.path') })" @click="copy(event.path, t('activity.detail.path'))" />
            </dd>
          </template>
          <template v-if="event.principal">
            <dt>{{ t('activity.detail.who') }}</dt>
            <dd>{{ event.principal }}</dd>
          </template>
          <template v-if="event.cache">
            <dt>{{ t('activity.detail.cache') }}</dt>
            <dd><CacheBadge :state="event.cache" /></dd>
          </template>
          <template v-if="event.bytes !== null && event.bytes !== undefined">
            <dt>{{ t('activity.detail.size') }}</dt>
            <dd><SizeCell :bytes="event.bytes" /></dd>
          </template>
          <template v-if="event.duration_ms !== null && event.duration_ms !== undefined">
            <dt>{{ t('activity.detail.duration') }}</dt>
            <dd class="num">{{ formatDuration(event.duration_ms / 1000) }}<span v-if="rate" class="text-faint"> · {{ rate }}</span></dd>
          </template>
          <dt>{{ t('activity.detail.id') }}</dt>
          <dd class="act-detail__copy">
            <span class="mono">{{ event.id }}</span>
            <q-btn flat dense round size="sm" :icon="iconCopy" :aria-label="t('common.copyWhat', { what: t('activity.detail.id') })" @click="copy(event.id, t('activity.detail.id'))" />
          </dd>
        </dl>

        <div class="section-label">{{ t('activity.detail.filterBy') }}</div>
        <div class="act-detail__actions">
          <q-btn v-if="event.repository" outline no-caps :icon="iconRepositories" :label="t('activity.detail.onlyRepo')" @click="narrow({ repo: event.repository })" />
          <q-btn v-if="event.principal" outline no-caps :icon="iconPerson" :label="t('activity.detail.onlyUser')" @click="narrow({ principal: event.principal })" />
          <q-btn outline no-caps :icon="meta.icon" :label="t('activity.detail.onlyKind')" @click="narrow({ kind: event.kind })" />
          <q-btn outline no-caps :icon="iconCalendar" :label="t('activity.detail.around')" @click="around" />
        </div>
      </div>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { ActivityEvent } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import RepoChip from '@/components/RepoChip.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useCopy } from '@/composables/useNotify'
import { t } from '@/i18n'
import { type ActivityFilters, activityTone, kindMeta } from '@/utils/activity'
import { formatLabel } from '@/utils/domain'
import { absoluteTime, formatDuration, formatRate } from '@/utils/format'
import { iconCalendar, iconClose, iconCopy, iconExpand, iconPerson, iconRepositories } from '@/utils/icons'

const props = withDefaults(defineProps<{ event: ActivityEvent | null; hasPrevious?: boolean; hasNext?: boolean }>(), { hasPrevious: false, hasNext: false })
const emit = defineEmits<{ close: []; step: [number]; narrow: [Partial<ActivityFilters>] }>()
const copy = useCopy()
const AROUND_MS = 5 * 60_000

const meta = computed(() => kindMeta(props.event?.kind ?? 'download'))
const outcomeTone = computed(() => (props.event?.outcome === 'failure' ? 'tone-error' : props.event?.outcome === 'success' ? 'tone-ok' : 'tone-offline'))
const rate = computed(() => {
  const e = props.event
  if (!e?.bytes || !e.duration_ms || e.duration_ms < 50) return ''
  return formatRate((e.bytes * 1000) / e.duration_ms)
})

function narrow(change: Partial<ActivityFilters>): void {
  emit('narrow', change)
}

function around(): void {
  if (!props.event) return
  const ts = Date.parse(props.event.ts)
  narrow({ range: null, from: new Date(ts - AROUND_MS).toISOString(), to: new Date(ts + AROUND_MS).toISOString() })
}
</script>

<style scoped>
.act-detail {
  width: 460px;
  max-width: 100vw;
  height: 100%;
  display: flex;
  flex-direction: column;
  border-radius: 0 !important;
}

.act-detail__head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px;
  border-bottom: 1px solid var(--border);
}

.act-detail__icon {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  flex: none;
}

.act-detail__titles {
  flex: 1;
  min-width: 0;
}

.act-detail__kind {
  font-size: var(--fs-16);
  font-weight: 650;
}

.act-detail__nav-up :deep(.q-icon) {
  transform: rotate(180deg);
}

.act-detail__body {
  padding: 16px;
  overflow-y: auto;
  flex: 1;
}

.act-detail__summary {
  margin: 0 0 16px;
  font-size: var(--fs-14);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.act-detail__summary--failure {
  color: var(--cache-error);
}

.act-detail__fields {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 10px 12px;
  margin: 0 0 20px;
  font-size: var(--fs-13);
}

.act-detail__fields dt {
  color: var(--text-secondary);
}

.act-detail__fields dd {
  margin: 0;
  min-width: 0;
}

.act-detail__wrap {
  overflow-wrap: anywhere;
}

.act-detail__copy {
  display: flex;
  align-items: flex-start;
  gap: 4px;
}

.act-detail__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}
</style>
