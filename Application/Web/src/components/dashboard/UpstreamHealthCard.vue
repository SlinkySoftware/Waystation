<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Upstream health per proxy repo: status, error rate, p95, last error, auto-block countdown, Docker Hub limit. -->
<template>
  <PageCard :title="t('dashboard.upstreams.title')" :caption="t('dashboard.upstreams.caption')" flush>
    <q-skeleton v-if="!rows.length && !loaded" height="200px" class="q-ma-md" />
    <ul v-else class="ups">
      <li v-for="u in rows" :key="u.repository" class="ups__row">
        <div class="ups__head">
          <RepoChip :name="u.repository" :format="u.format" :mark-override="repos.markFor(u.repository, u.format)" />
          <StatusPill :status="u.status" />
          <q-space />
          <span class="ups__metric" :title="t('dashboard.upstreams.errorRate')">
            <span class="text-faint">{{ t('dashboard.upstreams.err') }}</span> {{ formatPercent(u.error_rate) }}
          </span>
          <span v-if="u.p95_latency_ms !== null && u.p95_latency_ms !== undefined" class="ups__metric" :title="t('dashboard.upstreams.p95')">
            <span class="text-faint">p95</span> {{ formatLatency(u.p95_latency_ms) }}
          </span>
        </div>
        <div v-if="u.rate_limit" class="ups__gauge">
          <GaugeChart
            :value="u.rate_limit.remaining"
            :max="u.rate_limit.limit"
            :size="84"
            :tone="u.rate_limit.remaining / u.rate_limit.limit < 0.2 ? 'warning' : 'ok'"
            :label="t('dashboard.upstreams.rateLimit')"
            :caption="t('ui.gauge.ofLimit', { limit: u.rate_limit.limit })"
            :value-text="t('dashboard.upstreams.pullsLeft', { remaining: u.rate_limit.remaining, limit: u.rate_limit.limit })"
          />
          <div class="ups__gauge-text">
            <span>{{ t('dashboard.upstreams.pullsLeft', { remaining: u.rate_limit.remaining, limit: u.rate_limit.limit }) }}</span>
            <span v-if="u.rate_limit.reset_at" class="text-muted">{{ t('dashboard.upstreams.resetsIn', { time: formatDuration(Math.max(0, (Date.parse(u.rate_limit.reset_at) - now) / 1000)) }) }}</span>
          </div>
        </div>
        <div v-if="u.status === 'blocked' && u.blocked_until" class="ups__note ups__note--blocked">
          {{ t('dashboard.upstreams.blocked', { time: clock(Math.max(0, (Date.parse(u.blocked_until) - now) / 1000)) }) }}
        </div>
        <div v-if="u.last_error && u.status !== 'ok'" class="ups__note">
          {{ u.last_error }} · <TimeCell :value="u.last_error_at" />
        </div>
      </li>
    </ul>
  </PageCard>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'

import { api, call } from '@/api/client'
import type { UpstreamHealth } from '@/api/types'
import GaugeChart from '@/components/charts/GaugeChart.vue'
import PageCard from '@/components/PageCard.vue'
import RepoChip from '@/components/RepoChip.vue'
import StatusPill from '@/components/StatusPill.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { useLiveStore } from '@/stores/live'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatDuration, formatLatency, formatPercent } from '@/utils/format'

const live = useLiveStore()
const repos = useRepositoriesStore()
const { data } = useAsync(() => call(api.GET('/stats/upstreams')))
const loaded = computed(() => data.value !== null)

const ORDER = { blocked: 0, degraded: 1, ok: 2, offline: 3 }
const rows = computed<UpstreamHealth[]>(() =>
  (data.value ?? [])
    .map((u) => live.upstreams[u.repository] ?? u)
    .sort((a, b) => ORDER[a.status] - ORDER[b.status] || (b.rate_limit ? 1 : 0) - (a.rate_limit ? 1 : 0) || a.repository.localeCompare(b.repository)),
)

// 1 s clock for the countdowns.
const now = ref(Date.now())
const timer = setInterval(() => (now.value = Date.now()), 1000)
onBeforeUnmount(() => clearInterval(timer))

function clock(seconds: number): string {
  const s = Math.round(seconds)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}
</script>

<style scoped>
.ups {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 330px;
  overflow-y: auto;
}

.ups__row {
  padding: 10px 16px;
  border-top: 1px solid var(--border);
}

.ups__head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ups__metric {
  white-space: nowrap;
  font-size: var(--fs-12);
  font-variant-numeric: tabular-nums;
  min-width: 64px;
  text-align: right;
}

.ups__gauge {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 6px;
}

.ups__gauge-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--fs-12);
  font-variant-numeric: tabular-nums;
}

.ups__note {
  margin-top: 6px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.ups__note--blocked {
  color: var(--status-blocked);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
</style>
