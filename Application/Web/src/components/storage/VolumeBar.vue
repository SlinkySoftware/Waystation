<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- The data volume as one stacked bar (Waystation blobs, other files, free) with a legend carrying every value. -->
<template>
  <div class="volume">
    <div class="volume__bar" role="img" :aria-label="aria">
      <div class="volume__seg volume__seg--ws" :style="{ width: pct(v.waystation) }" />
      <div class="volume__seg volume__seg--other" :style="{ width: pct(v.other) }" />
    </div>
    <ul class="volume__legend">
      <li><span class="volume__key volume__seg--ws" />{{ t('storage.volume.waystation') }}<b>{{ formatBytes(v.waystation) }}</b><span class="text-faint">{{ pct(v.waystation) }}</span></li>
      <li><span class="volume__key volume__seg--other" />{{ t('storage.volume.other') }}<b>{{ formatBytes(v.other) }}</b><span class="text-faint">{{ pct(v.other) }}</span></li>
      <li><span class="volume__key volume__key--free" />{{ t('storage.volume.free') }}<b>{{ formatBytes(v.free) }}</b><span class="text-faint">{{ pct(v.free) }}</span></li>
    </ul>
    <div v-if="v.total && v.freeShare < 0.15" class="volume__warn" role="note">
      <q-icon :name="iconWarning" size="16px" />
      {{ t('storage.volume.warn') }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { t } from '@/i18n'
import { formatBytes } from '@/utils/format'
import { iconWarning } from '@/utils/icons'
import type { VolumeBreakdown } from '@/utils/storageStats'

const props = defineProps<{ volume: VolumeBreakdown }>()
const v = computed(() => props.volume)
const pct = (bytes: number) => (v.value.total ? `${((bytes / v.value.total) * 100).toFixed(1)}%` : '0%')
const aria = computed(
  () => `${t('storage.volume.aria')}: ${t('storage.volume.waystation')} ${formatBytes(v.value.waystation)}, ${t('storage.volume.other')} ${formatBytes(v.value.other)}, ${t('storage.volume.free')} ${formatBytes(v.value.free)}`,
)
</script>

<style scoped>
.volume__bar {
  display: flex;
  height: 14px;
  border-radius: 999px;
  background: var(--chart-track);
  overflow: hidden;
}

.volume__seg {
  height: 100%;
  transition: width 600ms var(--ease-out);
}

.volume__seg--ws {
  background: var(--q-primary);
}

.volume__seg--other {
  background: var(--chart-axis);
}

.volume__legend {
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
  margin: 12px 0 0;
  padding: 0;
  font-size: var(--fs-13);
}

.volume__legend li {
  display: flex;
  align-items: center;
  gap: 8px;
  font-variant-numeric: tabular-nums;
}

.volume__key {
  width: 10px;
  height: 10px;
  border-radius: 3px;
}

.volume__key--free {
  background: var(--chart-track);
  box-shadow: inset 0 0 0 1px var(--border-strong);
}

.volume__warn {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 12px;
  font-size: var(--fs-13);
  color: var(--status-degraded);
}

@media (prefers-reduced-motion: reduce) {
  .volume__seg {
    transition: none;
  }
}
</style>
