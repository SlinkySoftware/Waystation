<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Dashboard (UI_DESIGN §4.2): KPIs, live transfers (centrepiece), traffic, storage by format, upstream health,
     recent activity. Everything updates from the event stream. -->
<template>
  <q-page class="ws-page dash">
    <PageHeader :title="t('dashboard.title')" :caption="t('dashboard.caption')">
      <template #actions>
        <q-btn-toggle v-model="range" no-caps unelevated dense toggle-color="primary" class="range" :options="rangeOptions" :aria-label="t('dashboard.range')" />
      </template>
    </PageHeader>

    <KpiStrip :summary="summary.data.value" :range="range" />

    <div class="dash__grid">
      <PageCard class="dash__live" :title="t('live.title')" :caption="t('live.caption')" flush>
        <template #title>
          <span class="dash__pulse" aria-hidden="true" />
          {{ t('live.title') }}
        </template>
        <LiveTransfers :max-rows="$q.screen.lt.sm ? 6 : 10" :compact="$q.screen.lt.md" />
      </PageCard>
      <TrafficCard class="dash__traffic" :range="range" />
      <UpstreamHealthCard class="dash__upstreams" />
      <ActivityFeed class="dash__activity" :limit="7" />
      <StorageCard class="dash__storage" />
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, ref } from 'vue'

import { api, call } from '@/api/client'
import type { StatsRange } from '@/api/types'
import ActivityFeed from '@/components/dashboard/ActivityFeed.vue'
import KpiStrip from '@/components/dashboard/KpiStrip.vue'
import StorageCard from '@/components/dashboard/StorageCard.vue'
import TrafficCard from '@/components/dashboard/TrafficCard.vue'
import UpstreamHealthCard from '@/components/dashboard/UpstreamHealthCard.vue'
import LiveTransfers from '@/components/live/LiveTransfers.vue'
import PageCard from '@/components/PageCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'
import { readString, writeString } from '@/utils/storage'

const $q = useQuasar()
const RANGES: StatsRange[] = ['15m', '1h', '24h', '7d', '30d']
const stored = readString('ws.dashboard.range') as StatsRange | null
const range = ref<StatsRange>(stored && RANGES.includes(stored) ? stored : '24h')
const rangeOptions = computed(() => RANGES.map((r) => ({ label: t(`ranges.short.${r}`), value: r })))

const summary = useAsync(() => {
  writeString('ws.dashboard.range', range.value)
  return call(api.GET('/stats/summary', { params: { query: { range: range.value } } }))
}, [range])

void useRepositoriesStore().load().catch(() => undefined)
</script>

<style scoped>
.range {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
}

.range :deep(.q-btn) {
  padding: 0 12px;
  min-height: 32px;
}

.dash__grid {
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}

.dash__live {
  grid-column: span 12;
}

.dash__upstreams {
  grid-column: span 4;
}

.dash__traffic {
  grid-column: span 8;
}

.dash__storage {
  grid-column: span 4;
}

.dash__activity {
  grid-column: span 8;
}

.dash__pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--status-ok);
  box-shadow: 0 0 0 0 var(--status-ok);
  animation: ring 2s var(--ease-out) infinite;
}

@keyframes ring {
  0% {
    box-shadow: 0 0 0 0 color-mix(in srgb, var(--status-ok) 50%, transparent);
  }
  100% {
    box-shadow: 0 0 0 8px transparent;
  }
}

@media (max-width: 1439px) {
  .dash__traffic,
  .dash__activity {
    grid-column: span 12;
  }

  .dash__upstreams,
  .dash__storage {
    grid-column: span 6;
  }
}

@media (max-width: 1023px) {
  .dash__upstreams,
  .dash__storage {
    grid-column: span 12;
  }
}
</style>
