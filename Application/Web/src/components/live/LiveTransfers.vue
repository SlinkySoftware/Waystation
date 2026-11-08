<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!--
  The live transfers list (UI_DESIGN §4.2): rows animate in/out, finished rows fade after 5 s, failures stay 30 s
  (live store). Pausable (freezes the view, the stream keeps running); filter by format and repository.
-->
<template>
  <div class="live-xfers">
    <div class="live-xfers__toolbar">
      <div class="live-xfers__summary" aria-live="off">
        <span class="live-xfers__stat"><strong class="num">{{ activeCount }}</strong> {{ t('live.activeShort') }}</span>
        <span class="live-xfers__stat"><strong class="num">{{ formatRate(outRate) }}</strong> {{ t('live.toClients') }}</span>
        <span class="live-xfers__stat gt-xs"><strong class="num">{{ formatRate(upstreamRate) }}</strong> {{ t('live.fromUpstream') }}</span>
      </div>
      <q-space />
      <div class="live-xfers__filters">
        <q-btn-toggle
          v-if="!repository"
          v-model="format"
          no-caps
          dense
          unelevated
          toggle-color="primary"
          class="live-xfers__formats"
          :options="formatOptions"
          :aria-label="t('live.filterFormat')"
        />
        <q-select
          v-if="!repository"
          v-model="repo"
          dense
          outlined
          clearable
          emit-value
          map-options
          options-dense
          :options="repoOptions"
          :label="t('live.allRepos')"
          class="live-xfers__repo gt-xs"
        />
        <q-btn
          flat
          dense
          no-caps
          :icon="paused ? iconPlay : iconPause"
          :label="paused ? t('live.resume') : t('live.pause')"
          :aria-pressed="paused"
          @click="togglePause"
        />
      </div>
    </div>
    <div v-if="paused" class="live-xfers__paused" role="status">{{ t('live.pausedNote') }}</div>
    <div class="live-xfers__head gt-sm" :class="{ 'live-xfers__head--compact': compact }" role="row" aria-hidden="true">
      <span />
      <span>{{ t('live.colArtifact') }}</span>
      <span>{{ t('live.colCache') }}</span>
      <span>{{ t('live.colProgress') }}</span>
      <span class="text-right">{{ t('live.colRate') }}</span>
      <span v-if="!compact" class="text-right">{{ t('live.colEta') }}</span>
      <span v-if="!compact" class="text-right">{{ t('live.colLatency') }}</span>
    </div>
    <div class="live-xfers__rows" role="table" :aria-label="t('live.title')" :aria-rowcount="filtered.length">
      <TransitionGroup name="xfer">
        <TransferRow v-for="item in visible" :key="item.id" :transfer="item" :compact="compact" />
      </TransitionGroup>
      <EmptyState v-if="!filtered.length" compact :icon="iconSpeed" :title="t('live.emptyTitle')" :text="status === 'open' ? t('live.emptyText') : t('live.connecting')" />
    </div>
    <div v-if="hidden > 0" class="live-xfers__more">{{ t('live.more', { count: hidden }) }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, shallowRef } from 'vue'

import type { Format } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import { t } from '@/i18n'
import { type LiveTransfer, useLiveStore } from '@/stores/live'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatLabel, LISTED_FORMATS } from '@/utils/domain'
import { formatRate } from '@/utils/format'
import { iconPause, iconPlay, iconSpeed } from '@/utils/icons'

import TransferRow from './TransferRow.vue'

const props = withDefaults(defineProps<{ maxRows?: number; compact?: boolean; repository?: string | null }>(), {
  maxRows: 12,
  compact: false,
  repository: null,
})

const live = useLiveStore()
const status = computed(() => live.status)
const paused = ref(false)
const frozen = shallowRef<LiveTransfer[]>([])
const format = ref<Format | 'all'>('all')
const repo = ref<string | null>(props.repository)

const source = computed(() => {
  const all = paused.value ? frozen.value : live.transfers
  return props.repository ? all.filter((x) => x.repository === props.repository) : all
})
const repoOptions = computed(() => [...new Set(live.transfers.map((x) => x.repository))].sort().map((name) => ({ label: name, value: name })))
const repos = useRepositoriesStore()
/** Only the formats this instance has repositories of (all listed formats until the index has loaded). */
const formatOptions = computed(() => {
  const present = new Set(repos.items.map((r) => r.format))
  const shown = present.size ? LISTED_FORMATS.filter((f) => present.has(f)) : LISTED_FORMATS
  return [{ label: t('live.all'), value: 'all' }, ...shown.map((f) => ({ label: formatLabel(f), value: f }))]
})

const filtered = computed(() =>
  source.value.filter((x) => (format.value === 'all' || x.format === format.value) && (!repo.value || x.repository === repo.value)),
)
/** Active and failed rows first (failures are what you need to see), then the fading finished ones. */
const ordered = computed(() => {
  const rank = (x: LiveTransfer) => (x.state === 'failed' ? 0 : x.state === 'active' ? 1 : 2)
  return [...filtered.value].sort((a, b) => rank(a) - rank(b) || (a.started_at < b.started_at ? 1 : -1))
})
const visible = computed(() => ordered.value.slice(0, props.maxRows))
const hidden = computed(() => Math.max(0, ordered.value.length - props.maxRows))
const activeCount = computed(() => source.value.filter((x) => x.state === 'active').length)
const outRate = computed(() => source.value.filter((x) => x.state === 'active').reduce((a, x) => a + x.rate_bps, 0))
const upstreamRate = computed(() => source.value.filter((x) => x.state === 'active' && x.cache === 'FETCHING').reduce((a, x) => a + x.rate_bps, 0))

function togglePause(): void {
  if (!paused.value) frozen.value = live.transfers
  paused.value = !paused.value
}
</script>

<style scoped>
.live-xfers__toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 16px;
  padding: 0 16px 12px;
}

.live-xfers__summary {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.live-xfers__stat strong {
  color: var(--text-primary);
  font-weight: 650;
  margin-right: 2px;
}

.live-xfers__filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.live-xfers__formats {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.live-xfers__formats :deep(.q-btn) {
  padding: 0 10px;
  min-height: 30px;
  font-size: var(--fs-13);
}

.live-xfers__repo {
  min-width: 170px;
}

.live-xfers__repo :deep(.q-field__control),
.live-xfers__repo :deep(.q-field__marginal) {
  height: 32px;
  min-height: 32px;
}

.live-xfers__paused {
  margin: 0 16px 8px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: var(--status-degraded-bg);
  color: var(--status-degraded);
  font-size: var(--fs-12);
  font-weight: 600;
}

.live-xfers__head {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) 118px minmax(150px, 220px) 84px 64px 60px;
  gap: 12px;
  padding: 6px 16px;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  background: var(--surface-subtle);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

.live-xfers__head--compact {
  display: none;
}

.live-xfers__rows {
  position: relative;
  overflow: hidden;
  min-height: 120px;
}

.live-xfers__more {
  padding: 8px 16px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.xfer-enter-active {
  transition: opacity 220ms var(--ease-out), transform 220ms var(--ease-out);
}

.xfer-leave-active {
  transition: opacity 260ms var(--ease-out);
  position: absolute;
  left: 0;
  right: 0;
}

.xfer-enter-from {
  opacity: 0;
  transform: translateY(-6px);
}

.xfer-leave-to {
  opacity: 0;
}

.xfer-move {
  transition: transform 260ms var(--ease-out);
}
</style>
