<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Donut with a centre total and a legend carrying every value (identity never by colour alone). -->
<template>
  <div class="donut">
    <svg :width="size" :height="size" :viewBox="`0 0 ${size} ${size}`" role="img" :aria-label="label">
      <circle :cx="c" :cy="c" :r="(outer + inner) / 2" :stroke-width="outer - inner" class="donut__track" />
      <path
        v-for="(seg, i) in segments"
        :key="items[i]!.key"
        :d="seg"
        :fill="items[i]!.color"
        class="donut__seg"
        :class="{ dim: active !== null && active !== i }"
        @pointerenter="active = i"
        @pointerleave="active = null"
      />
      <text :x="c" :y="c - 2" text-anchor="middle" class="donut__value">{{ centreValue }}</text>
      <text :x="c" :y="c + 16" text-anchor="middle" class="donut__caption">{{ centreCaption }}</text>
    </svg>
    <ul class="donut__legend">
      <li v-for="(item, i) in items" :key="item.key" :class="{ dim: active !== null && active !== i }" @pointerenter="active = i" @pointerleave="active = null">
        <span class="donut__key" :style="{ background: item.color }" />
        <span class="donut__label"><slot name="label" :item="item">{{ item.label }}</slot></span>
        <span class="donut__amount">{{ format(item.value) }}</span>
        <span class="donut__share">{{ share(item.value) }}</span>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { arcPath, arcs } from './scale'

export interface DonutItem {
  key: string
  label: string
  value: number
  color: string
}

const props = withDefaults(defineProps<{ items: DonutItem[]; label: string; format: (v: number) => string; size?: number; caption?: string }>(), {
  size: 152,
  caption: '',
})

const active = ref<number | null>(null)
const c = computed(() => props.size / 2)
const outer = computed(() => props.size / 2 - 2)
const inner = computed(() => outer.value - Math.max(14, props.size * 0.13))
const total = computed(() => props.items.reduce((a, i) => a + Math.max(0, i.value), 0))
const segments = computed(() => arcs(props.items.map((i) => i.value), 0.035).map((a) => arcPath(c.value, c.value, outer.value, inner.value, a.start, a.end)))
const centreValue = computed(() => props.format(active.value !== null ? props.items[active.value]!.value : total.value))
const centreCaption = computed(() => (active.value !== null ? props.items[active.value]!.label : props.caption))
const share = (v: number) => (total.value ? `${((v / total.value) * 100).toFixed(v / total.value < 0.1 ? 1 : 0)}%` : '–')
</script>

<style scoped>
.donut {
  display: flex;
  align-items: center;
  gap: 20px;
  flex-wrap: wrap;
}

svg {
  flex: none;
}

.donut__track {
  fill: none;
  stroke: var(--chart-track);
}

.donut__seg {
  transition: opacity var(--dur) var(--ease-out);
}

.dim {
  opacity: 0.4;
}

.donut__value {
  fill: var(--text-primary);
  font-size: 17px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}

.donut__caption {
  fill: var(--text-secondary);
  font-size: 11px;
}

.donut__legend {
  list-style: none;
  margin: 0;
  padding: 0;
  flex: 1;
  min-width: 180px;
}

.donut__legend li {
  display: grid;
  grid-template-columns: 12px 1fr auto 44px;
  align-items: center;
  gap: 8px;
  padding: 5px 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--fs-13);
  transition: opacity var(--dur) var(--ease-out);
}

.donut__legend li:last-child {
  border-bottom: 0;
}

.donut__key {
  width: 10px;
  height: 10px;
  border-radius: 3px;
}

.donut__label {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.donut__amount {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.donut__share {
  text-align: right;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
</style>
