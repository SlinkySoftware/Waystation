<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Every base component with fixed sample data (UI_DESIGN §2, T-U01). Rendered twice by the dev showcase page,
     once per theme; no API calls, no timers, so it doubles as a visual-regression fixture. -->
<template>
  <div class="gallery">
    <PageCard :title="t('ui.showcase.tokens')" :caption="t('ui.showcase.tokensCaption')">
      <div class="swatches">
        <div v-for="s in SWATCHES" :key="s" class="swatch">
          <span class="swatch__chip" :style="{ background: `var(--${s})` }" />
          <span class="mono swatch__name">--{{ s }}</span>
        </div>
      </div>
      <div class="type-scale">
        <span v-for="size in SIZES" :key="size" :style="{ fontSize: `var(--fs-${size})` }">{{ size }} px Waystation</span>
        <span class="mono">sha256:9f86d081884c7d65 · /v2/library/nginx</span>
      </div>
    </PageCard>

    <PageCard :title="t('ui.showcase.buttons')">
      <div class="row-wrap">
        <q-btn unelevated no-caps color="primary" :label="t('ui.showcase.primary')" />
        <q-btn outline no-caps color="primary" :label="t('ui.showcase.secondary')" />
        <q-btn flat no-caps :label="t('ui.showcase.flat')" />
        <q-btn flat round dense :icon="iconCopy" :aria-label="t('common.copy')" />
        <q-btn unelevated no-caps color="primary" :label="t('ui.showcase.dialog')" @click="dialogOpen = true" />
      </div>
    </PageCard>

    <PageCard :title="t('ui.showcase.badges')">
      <div class="row-wrap">
        <StatusPill v-for="s in STATUSES" :key="s" :status="s" :tooltip="false" />
      </div>
      <div class="row-wrap">
        <CacheBadge v-for="c in CACHE_STATES" :key="c" :state="c" :followers="c === 'COALESCED' ? 3 : 0" />
      </div>
      <div class="row-wrap">
        <RepoChip name="dockerhub-proxy" format="docker" :link="false" />
        <RepoChip name="npm" format="npm" :link="false" />
        <RepoChip name="debian-security" format="apt" :link="false" />
        <ManagedBadge :item="{ managed_by: 'provisioning' }" />
      </div>
    </PageCard>

    <PageCard :title="t('ui.showcase.formatIcons')">
      <div class="marks">
        <span v-for="m in MARKS" :key="m" class="marks__item">
          <FormatIcon :mark="m" :size="22" brand />
          <span class="marks__name">{{ m }}</span>
        </span>
      </div>
    </PageCard>

    <PageCard :title="t('ui.showcase.cells')">
      <dl class="cells">
        <div><dt>SizeCell</dt><dd><SizeCell :bytes="734003200" /></dd></div>
        <div><dt>TimeCell</dt><dd><TimeCell value="2026-11-20T08:15:00Z" /></dd></div>
        <div><dt>CopyField</dt><dd><CopyField value="https://registry.example.com/dockerhub-proxy/" :label="t('repos.url')" /></dd></div>
        <div><dt>QuotaMeter</dt><dd><QuotaMeter :quota="QUOTA" /></dd></div>
      </dl>
    </PageCard>

    <PageCard :title="t('ui.showcase.charts')" :caption="t('ui.showcase.chartsCaption')">
      <div class="charts">
        <div class="charts__cell">
          <div class="section-label">Sparkline</div>
          <Sparkline :values="SPARK" :height="40" />
        </div>
        <div class="charts__cell">
          <div class="section-label">GaugeChart</div>
          <div class="row-wrap">
            <GaugeChart :value="142" :max="200" :label="t('dashboard.upstreams.rateLimit')" :caption="t('ui.gauge.ofLimit', { limit: 200 })" />
            <GaugeChart :value="31" :max="200" tone="warning" :marks="[0.8]" :label="t('dashboard.upstreams.rateLimit')" :caption="t('ui.gauge.ofLimit', { limit: 200 })" />
          </div>
        </div>
        <div class="charts__cell">
          <div class="section-label">DonutChart</div>
          <DonutChart :items="DONUT" :label="t('ui.showcase.storageByFormat')" :format="formatBytes" :size="132" />
        </div>
        <div class="charts__cell">
          <div class="section-label">BarList</div>
          <BarList :items="BARS" :label="t('ui.showcase.largest')" :format="formatBytes" />
        </div>
        <div class="charts__cell charts__cell--wide">
          <div class="section-label">StackedAreaChart</div>
          <StackedAreaChart :timestamps="TIMES" :series="AREA" :step-seconds="300" :label="t('ui.showcase.traffic')" :format="formatBytes" :height="180" />
        </div>
      </div>
    </PageCard>

    <PageCard :title="t('ui.showcase.live')" flush>
      <TransferRow v-for="x in TRANSFERS" :key="x.id" :transfer="x" />
    </PageCard>

    <PageCard :title="t('ui.showcase.empty')">
      <div class="empties">
        <EmptyState compact illustration="cache" :title="t('ui.showcase.emptyTitle')" :text="t('ui.showcase.emptyText')" />
        <LoadFailedState compact :error="new Error('upstream timed out')" :title="t('ui.showcase.failedTitle')" />
      </div>
    </PageCard>

    <FormDialog v-model="dialogOpen" :title="t('ui.showcase.dialog')" :caption="t('ui.showcase.dialogCaption')" @submit="dialogOpen = false">
      <q-input outlined dense :label="t('repoForm.name')" model-value="dockerhub-proxy" />
    </FormDialog>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import type { CacheState, RepositoryQuota, RepositoryStatus } from '@/api/types'
import { brandMarks, type BrandMark } from '@/components/brandMarks'
import CacheBadge from '@/components/CacheBadge.vue'
import BarList, { type BarItem } from '@/components/charts/BarList.vue'
import DonutChart, { type DonutItem } from '@/components/charts/DonutChart.vue'
import GaugeChart from '@/components/charts/GaugeChart.vue'
import Sparkline from '@/components/charts/Sparkline.vue'
import StackedAreaChart, { type AreaSeries } from '@/components/charts/StackedAreaChart.vue'
import CopyField from '@/components/CopyField.vue'
import EmptyState from '@/components/EmptyState.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import FormDialog from '@/components/FormDialog.vue'
import TransferRow from '@/components/live/TransferRow.vue'
import LoadFailedState from '@/components/LoadFailedState.vue'
import ManagedBadge from '@/components/ManagedBadge.vue'
import PageCard from '@/components/PageCard.vue'
import RepoChip from '@/components/RepoChip.vue'
import QuotaMeter from '@/components/repositories/QuotaMeter.vue'
import SizeCell from '@/components/SizeCell.vue'
import StatusPill from '@/components/StatusPill.vue'
import TimeCell from '@/components/TimeCell.vue'
import { t } from '@/i18n'
import type { LiveTransfer } from '@/stores/live'
import { formatBytes } from '@/utils/format'
import { iconCopy } from '@/utils/icons'

const dialogOpen = ref(false)

const SWATCHES = ['brand-500', 'brand-400', 'status-ok', 'status-degraded', 'status-blocked', 'status-offline', 'cache-hit', 'cache-miss', 'cache-stale', 'cache-error', 'chart-1', 'chart-2', 'chart-3', 'chart-4', 'chart-5', 'chart-6', 'chart-7', 'chart-8']
const SIZES = [12, 13, 14, 16, 20, 24]
const STATUSES: RepositoryStatus[] = ['ok', 'degraded', 'blocked', 'offline']
const CACHE_STATES: CacheState[] = ['HIT', 'FETCHING', 'COALESCED', 'STALE', 'REVALIDATED', 'ERROR']
const MARKS = Object.keys(brandMarks) as BrandMark[]
const QUOTA: RepositoryQuota = { quota_bytes: 10 * 1024 ** 3, mode: 'soft', used_bytes: 8.6 * 1024 ** 3, used_ratio: 0.86, state: 'warning' }
const SPARK = [4, 6, 5, 9, 12, 10, 14, 13, 17, 15, 19, 22, 20, 24]
const DONUT: DonutItem[] = [
  { key: 'docker', label: 'Docker', value: 412 * 1024 ** 3, color: 'var(--chart-1)' },
  { key: 'apt', label: 'apt', value: 186 * 1024 ** 3, color: 'var(--chart-2)' },
  { key: 'npm', label: 'npm', value: 64 * 1024 ** 3, color: 'var(--chart-3)' },
  { key: 'yum', label: 'yum', value: 41 * 1024 ** 3, color: 'var(--chart-4)' },
]
const BARS: BarItem[] = [
  { key: 'dockerhub-proxy', label: 'dockerhub-proxy', value: 301 * 1024 ** 3 },
  { key: 'debian', label: 'debian', value: 122 * 1024 ** 3 },
  { key: 'ghcr-proxy', label: 'ghcr-proxy', value: 111 * 1024 ** 3 },
  { key: 'npm', label: 'npm', value: 64 * 1024 ** 3 },
]
const BASE = Date.UTC(2026, 10, 20, 8, 0, 0)
const TIMES = Array.from({ length: 24 }, (_, i) => new Date(BASE + i * 300_000).toISOString())
const AREA: AreaSeries[] = [
  { key: 'hit', label: t('ui.showcase.hit'), color: 'var(--chart-hit)', values: TIMES.map((_, i) => (40 + 25 * Math.sin(i / 3) + i * 2) * 1024 ** 2) },
  { key: 'upstream', label: t('ui.showcase.upstream'), color: 'var(--chart-upstream)', values: TIMES.map((_, i) => (12 + 8 * Math.cos(i / 4)) * 1024 ** 2) },
]
const STARTED = new Date(BASE).toISOString()
function transfer(id: string, cache: CacheState, done: number, total: number, state: LiveTransfer['state'], artifact: string, repository: string, format: LiveTransfer['format']): LiveTransfer {
  return { id, repository, format, direction: 'download', artifact, client: { ip: '192.0.2.24', user: 'ci-runner' }, cache, followers: cache === 'COALESCED' ? 2 : 0, bytes_done: done, bytes_total: total, rate_bps: 48_000_000, started_at: STARTED, updated_at: STARTED, state, endedAt: null }
}
const TRANSFERS: LiveTransfer[] = [
  transfer('a', 'FETCHING', 38 * 1024 ** 2, 96 * 1024 ** 2, 'active', 'library/postgres/blobs/sha256:4f4fb700ef54', 'dockerhub-proxy', 'docker'),
  transfer('b', 'HIT', 2 * 1024 ** 2, 2 * 1024 ** 2, 'finished', 'pool/main/o/openssl/libssl3t64_3.5.4-1_amd64.deb', 'debian', 'apt'),
  transfer('c', 'ERROR', 0, 4 * 1024 ** 2, 'failed', 'vite/-/vite-8.3.6.tgz', 'npm', 'npm'),
]
</script>

<style scoped>
.gallery {
  display: grid;
  gap: 16px;
  min-width: 0;
}

.row-wrap {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.row-wrap + .row-wrap {
  margin-top: 10px;
}

.swatches {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 8px;
}

.swatch {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-12);
}

.swatch__chip {
  width: 22px;
  height: 22px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}

.swatch__name {
  color: var(--text-secondary);
}

.type-scale {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 14px;
}

.marks {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 10px;
}

.marks__item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.cells {
  display: grid;
  gap: 12px;
  margin: 0;
}

.cells dt {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.cells dd {
  margin: 2px 0 0;
}

.charts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 20px;
}

.charts__cell {
  display: grid;
  gap: 8px;
  align-content: start;
  min-width: 0;
}

.charts__cell--wide {
  grid-column: 1 / -1;
}

.empties {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}
</style>
