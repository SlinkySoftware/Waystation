<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- KPI tile: label, headline value, one line of context, sparkline over the selected range. -->
<template>
  <div class="kpi q-card" :class="{ 'kpi--live': live }">
    <div class="kpi__label">
      <span>{{ label }}</span>
      <span v-if="live" class="kpi__live" :title="t('dashboard.liveValue')">{{ t('dashboard.live') }}</span>
    </div>
    <div class="kpi__value">
      <q-skeleton v-if="loading" type="text" width="70%" height="34px" />
      <template v-else>{{ value }}<span v-if="unit" class="kpi__unit">{{ unit }}</span></template>
    </div>
    <div class="kpi__sub">
      <slot name="sub">{{ sub }}</slot>
    </div>
    <div v-if="meter !== null" class="kpi__meter" role="meter" :aria-valuenow="Math.round(meter * 100)" aria-valuemin="0" aria-valuemax="100" :aria-label="meterLabel">
      <div class="kpi__meter-fill" :class="{ 'kpi__meter-fill--warn': meter > 0.85 }" :style="{ width: `${Math.min(100, meter * 100)}%` }" />
    </div>
    <Sparkline v-else class="kpi__spark" :values="spark" :color="color" :height="30" :min="sparkMin" />
  </div>
</template>

<script setup lang="ts">
import Sparkline from '@/components/charts/Sparkline.vue'
import { t } from '@/i18n'

withDefaults(
  defineProps<{
    label: string
    value: string
    unit?: string
    sub?: string
    spark?: (number | null)[]
    color?: string
    live?: boolean
    loading?: boolean
    /** 0..1: show a usage bar instead of a sparkline. */
    meter?: number | null
    meterLabel?: string
    sparkMin?: number | null
  }>(),
  { unit: '', sub: '', spark: () => [], color: 'var(--chart-1)', live: false, loading: false, meter: null, meterLabel: '', sparkMin: 0 },
)
</script>

<style scoped>
.kpi {
  display: flex;
  flex-direction: column;
  padding: 14px 16px 12px;
  min-width: 0;
  min-height: 132px;
}

.kpi__label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-13);
  font-weight: 550;
  color: var(--text-secondary);
}

.kpi__live {
  font-size: 10px;
  font-weight: 650;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--status-ok);
}

.kpi__value {
  margin-top: 6px;
  font-size: 28px;
  font-weight: 650;
  line-height: 1.15;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.kpi__unit {
  margin-left: 4px;
  font-size: 15px;
  font-weight: 550;
  color: var(--text-secondary);
  letter-spacing: 0;
}

.kpi__sub {
  margin-top: 2px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}

.kpi__spark {
  margin-top: auto;
  padding-top: 8px;
}

.kpi__meter {
  margin-top: auto;
  height: 6px;
  border-radius: 999px;
  background: var(--chart-track);
  overflow: hidden;
}

.kpi__meter-fill {
  height: 100%;
  border-radius: 999px;
  background: var(--q-primary);
  transition: width 600ms var(--ease-out);
}

.kpi__meter-fill--warn {
  background: var(--status-degraded);
}
</style>
