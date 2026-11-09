<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Repository overview: live transfers here, traffic, upstream configuration + health, recent activity. -->
<template>
  <div class="overview">
    <PageCard class="overview__live" :title="t('live.title')" :caption="t('detail.liveCaption')" flush>
      <LiveTransfers :key="repo.name" :repository="repo.name" :max-rows="6" :compact="$q.screen.lt.lg" />
    </PageCard>

    <TrafficCard class="overview__traffic" :range="range" :repository="repo.name" :height="220" />

    <PageCard class="overview__upstream" :title="t('detail.upstream.title')">
      <dl class="facts">
        <dt>{{ t('detail.upstream.url') }}</dt>
        <dd><CopyField :value="repo.proxy?.upstream_url ?? '–'" compact /></dd>
        <template v-if="health">
          <dt>{{ t('detail.upstream.health') }}</dt>
          <dd class="facts__inline"><StatusPill :status="health.status" /> {{ t('detail.upstream.healthLine', { err: formatPercent(health.error_rate), p95: formatLatency(health.p95_latency_ms) }) }}</dd>
          <template v-if="health.status === 'blocked' && health.blocked_until">
            <dt>{{ t('detail.upstream.autoBlocked') }}</dt>
            <dd class="facts__blocked">{{ t('dashboard.upstreams.blocked', { time: clock(Math.max(0, (Date.parse(health.blocked_until) - now) / 1000)) }) }}</dd>
          </template>
          <template v-if="health.rate_limit">
            <dt>{{ t('detail.upstream.rateLimit') }}</dt>
            <dd>{{ t('dashboard.upstreams.pullsLeft', { remaining: health.rate_limit.remaining, limit: health.rate_limit.limit }) }}</dd>
          </template>
          <template v-if="health.last_error">
            <dt>{{ t('detail.upstream.lastError') }}</dt>
            <dd>{{ health.last_error }} · <TimeCell :value="health.last_error_at" /></dd>
          </template>
        </template>
        <dt>{{ t('detail.upstream.auth') }}</dt>
        <dd>{{ authLabel }}</dd>
        <template v-if="aliases.length">
          <dt>{{ t('repoForm.aliases') }}</dt>
          <dd class="facts__inline"><span v-for="a in aliases" :key="a" class="facts__tag mono">{{ a }}</span></dd>
        </template>
        <dt>{{ t('detail.upstream.freshness') }}</dt>
        <dd>{{ t('detail.upstream.freshnessLine', { meta: formatDuration(repo.proxy?.metadata_max_age_s ?? 0), neg: formatDuration(repo.proxy?.negative_ttl_s ?? 0) }) }}</dd>
        <dt>{{ t('detail.upstream.behaviour') }}</dt>
        <dd>{{ behaviour }}</dd>
      </dl>
    </PageCard>

    <ActivityFeed class="overview__activity" :repository="repo.name" :limit="6" />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'

import { api, call } from '@/api/client'
import type { DockerOptions, Repository, StatsRange } from '@/api/types'
import CopyField from '@/components/CopyField.vue'
import ActivityFeed from '@/components/dashboard/ActivityFeed.vue'
import TrafficCard from '@/components/dashboard/TrafficCard.vue'
import LiveTransfers from '@/components/live/LiveTransfers.vue'
import PageCard from '@/components/PageCard.vue'
import StatusPill from '@/components/StatusPill.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { useLiveStore } from '@/stores/live'
import { formatDuration, formatLatency, formatPercent } from '@/utils/format'

const props = defineProps<{ repo: Repository }>()

const range = ref<StatsRange>('24h')
const live = useLiveStore()
const upstreams = useAsync(() => call(api.GET('/stats/upstreams')))
const health = computed(() => live.upstreams[props.repo.name] ?? upstreams.data.value?.find((u) => u.repository === props.repo.name) ?? null)
const aliases = computed(() => (props.repo.format === 'docker' ? ((props.repo.format_options as DockerOptions | undefined)?.aliases ?? []) : []))

const authLabel = computed(() => {
  const a = props.repo.proxy?.auth
  if (!a || a.kind === 'none') return t('repoForm.authNone')
  const kind = a.kind === 'basic' ? t('repoForm.authBasic') : t('repoForm.authBearer')
  return `${kind}${a.username ? ` (${a.username})` : ''} · ${a.secret_set ? t('detail.upstream.secretStored') : t('detail.upstream.secretMissing')}`
})

// 1 s clock for the auto-block countdown.
const now = ref(Date.now())
const timer = setInterval(() => (now.value = Date.now()), 1000)
onBeforeUnmount(() => clearInterval(timer))

function clock(seconds: number): string {
  const s = Math.round(seconds)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

const behaviour = computed(() => {
  const p = props.repo.proxy
  const block = p?.auto_block
  const parts = [
    p?.stale_on_error ? t('detail.upstream.stale') : null,
    p?.offline ? t('detail.upstream.offlineMode') : null,
    p?.allow_private_upstream ? t('detail.upstream.private') : null,
    block?.enabled !== false ? t('detail.upstream.autoBlock', { n: block?.failures ?? 5 }) : null,
    p?.http_proxy ? t('detail.upstream.viaProxy') : null,
    p?.ca_pem ? t('detail.upstream.customCa') : null,
  ]
  return parts.filter(Boolean).join(' · ') || '–'
})
</script>

<style scoped>
.overview {
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: 12px;
}

.overview__live {
  grid-column: span 12;
}

.overview__traffic {
  grid-column: span 7;
}

.overview__upstream {
  grid-column: span 5;
}

.overview__activity {
  grid-column: span 12;
}

.facts {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 8px 16px;
  margin: 0;
  font-size: var(--fs-13);
}

.facts dt {
  color: var(--text-secondary);
}

.facts dd {
  margin: 0;
  min-width: 0;
}

.facts__inline {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.facts__blocked {
  color: var(--status-blocked);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.facts__tag {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--surface-sunken);
  border: 1px solid var(--border);
}

@media (max-width: 1023px) {
  .overview__traffic,
  .overview__upstream {
    grid-column: span 12;
  }
}
</style>
