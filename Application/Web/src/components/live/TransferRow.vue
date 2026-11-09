<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- One live transfer: format, artifact, repo/client, cache state, progress, throughput, ETA, upstream latency. -->
<template>
  <div class="xfer" :class="[`xfer--${transfer.state}`, { 'xfer--compact': compact }]" role="row">
    <div class="xfer__icon" role="cell">
      <FormatIcon :mark="mark" :size="18" :brand="transfer.state === 'active'" />
    </div>
    <div class="xfer__main" role="cell">
      <div class="xfer__artifact mono" :title="transfer.path ?? transfer.artifact">{{ shortName }}</div>
      <div class="xfer__meta">
        <RepoChip :name="transfer.repository" :format="transfer.format" :mark-override="mark" />
        <span v-if="transfer.component" class="xfer__component">{{ transfer.component }}</span>
        <span class="xfer__client" :title="transfer.client.agent ?? undefined">{{ transfer.client.user ?? transfer.client.ip }}</span>
      </div>
    </div>
    <div class="xfer__cache" role="cell">
      <span v-if="transfer.direction === 'upload' && transfer.cache !== 'ERROR'" class="ws-badge xfer__upload tone-ok">
        <q-icon :name="iconUpload" />
        <span>{{ t('live.upload') }}</span>
        <q-tooltip>{{ t('live.uploadHint') }}</q-tooltip>
      </span>
      <CacheBadge v-else :state="transfer.cache" :followers="transfer.followers ?? 0" />
    </div>
    <div class="xfer__progress" role="cell">
      <div class="xfer__bar" :class="`xfer__bar--${barTone}`" role="progressbar" :aria-valuenow="Math.round(pct)" aria-valuemin="0" aria-valuemax="100" :aria-label="t('live.progress')">
        <div class="xfer__fill" :style="{ transform: `scaleX(${pct / 100})` }" />
      </div>
      <div class="xfer__bytes">
        <span>{{ formatBytes(transfer.bytes_done) }}<template v-if="transfer.bytes_total"> / {{ formatBytes(transfer.bytes_total) }}</template></span>
        <span class="xfer__pct">{{ transfer.state === 'failed' ? t('live.failed') : transfer.state === 'finished' ? t('live.done') : `${Math.floor(pct)}%` }}</span>
      </div>
    </div>
    <div class="xfer__rate num" role="cell">{{ transfer.state === 'failed' ? '–' : formatRate(transfer.rate_bps) }}</div>
    <div class="xfer__eta num" role="cell">{{ eta }}</div>
    <div class="xfer__latency num" role="cell" :title="t('live.upstreamLatency')">{{ transfer.upstream_latency_ms !== null && transfer.upstream_latency_ms !== undefined ? formatLatency(transfer.upstream_latency_ms) : '–' }}</div>
    <div v-if="transfer.state === 'failed' && transfer.error" class="xfer__error" role="cell">{{ transfer.error }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import CacheBadge from '@/components/CacheBadge.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import RepoChip from '@/components/RepoChip.vue'
import { t } from '@/i18n'
import type { LiveTransfer } from '@/stores/live'
import { useRepositoriesStore } from '@/stores/repositories'
import { etaSeconds, formatBytes, formatDuration, formatLatency, formatRate, truncateMiddle } from '@/utils/format'
import { iconUpload } from '@/utils/icons'

const props = withDefaults(defineProps<{ transfer: LiveTransfer; compact?: boolean }>(), { compact: false })

const repos = useRepositoriesStore()
const mark = computed(() => repos.markFor(props.transfer.repository, props.transfer.format))
const shortName = computed(() => truncateMiddle(props.transfer.artifact, props.compact ? 40 : 64))
const pct = computed(() => {
  const t = props.transfer
  if (t.state === 'finished') return 100
  if (!t.bytes_total) return 0
  return Math.min(100, (t.bytes_done / t.bytes_total) * 100)
})
const barTone = computed(() => {
  const t = props.transfer
  if (t.state === 'failed' || t.cache === 'ERROR') return 'error'
  if (t.direction === 'upload') return 'hit'
  if (t.cache === 'FETCHING' || t.cache === 'COALESCED') return 'miss'
  if (t.cache === 'STALE') return 'stale'
  return 'hit'
})
const eta = computed(() => {
  const t = props.transfer
  if (t.state !== 'active') return '–'
  const s = etaSeconds(t.bytes_done, t.bytes_total, t.rate_bps)
  return s === null ? '–' : s < 1 ? '<1 s' : formatDuration(s)
})
</script>

<style scoped>
.xfer {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) 118px minmax(150px, 220px) 84px 64px 60px;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  transition: background 600ms var(--ease-out), opacity 600ms var(--ease-out);
}

.xfer--finished {
  animation: settle 5s linear forwards;
}

.xfer--failed {
  background: var(--cache-error-bg);
}

.xfer__icon {
  display: grid;
  place-items: center;
}

.xfer__main {
  min-width: 0;
}

.xfer__artifact {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-size: var(--fs-13);
  color: var(--text-primary);
}

.xfer__meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 3px;
  min-width: 0;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
}

.xfer__meta :deep(.repo-chip) {
  height: 19px;
  font-size: 11px;
  flex: none;
}

.xfer__component,
.xfer__client {
  overflow: hidden;
  text-overflow: ellipsis;
}

.xfer__client::before {
  content: '·';
  margin-right: 8px;
  color: var(--text-tertiary);
}

.xfer__bar {
  height: 6px;
  border-radius: 999px;
  background: var(--chart-track);
  overflow: hidden;
}

.xfer__fill {
  height: 100%;
  width: 100%;
  transform-origin: left center;
  border-radius: 999px;
  background: var(--cache-hit);
  transition: transform 260ms linear;
}

.xfer__bar--miss .xfer__fill {
  background: var(--cache-miss);
}

.xfer__bar--stale .xfer__fill {
  background: var(--cache-stale);
}

.xfer__bar--error .xfer__fill {
  background: var(--cache-error);
}

.xfer__bytes {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.xfer__pct {
  color: var(--text-primary);
  font-weight: 600;
}

.xfer__rate,
.xfer__eta,
.xfer__latency {
  text-align: right;
  font-size: var(--fs-13);
  white-space: nowrap;
}

.xfer__eta,
.xfer__latency {
  color: var(--text-secondary);
}

.xfer__error {
  grid-column: 2 / -1;
  margin-top: -4px;
  font-size: var(--fs-12);
  color: var(--cache-error);
}

/* Narrower containers (drawer, tablets): drop ETA and latency. */
.xfer--compact,
.xfer--compact.xfer {
  grid-template-columns: 24px minmax(0, 1fr) 108px 84px;
  grid-template-areas:
    'icon main cache rate'
    'icon progress progress progress';
  row-gap: 6px;
}

.xfer--compact .xfer__icon {
  grid-area: icon;
  align-self: start;
  padding-top: 2px;
}

.xfer--compact .xfer__main {
  grid-area: main;
}

.xfer__upload {
  font-size: 11px;
  letter-spacing: 0.02em;
  height: 20px;
  padding: 0 7px;
}

.xfer--compact .xfer__cache {
  grid-area: cache;
}

.xfer--compact .xfer__rate {
  grid-area: rate;
}

.xfer--compact .xfer__progress {
  grid-area: progress;
}

.xfer--compact .xfer__eta,
.xfer--compact .xfer__latency {
  display: none;
}

.xfer--compact .xfer__error {
  grid-column: 2 / -1;
  margin-top: 0;
}

@media (max-width: 599px) {
  .xfer,
  .xfer--compact.xfer {
    grid-template-columns: minmax(0, 1fr) 76px;
    grid-template-areas:
      'main rate'
      'progress progress';
    padding: 8px 12px;
  }

  .xfer__icon,
  .xfer__cache,
  .xfer__eta,
  .xfer__latency,
  .xfer__meta {
    display: none;
  }

  .xfer__main {
    grid-area: main;
  }

  .xfer__rate {
    grid-area: rate;
  }

  .xfer__progress {
    grid-area: progress;
  }
}

@keyframes settle {
  0%,
  60% {
    opacity: 1;
  }
  100% {
    opacity: 0.35;
  }
}
</style>
