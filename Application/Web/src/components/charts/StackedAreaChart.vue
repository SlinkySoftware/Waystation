<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!--
  Stacked area time series in SVG: one y axis, hairline grid, legend, crosshair tooltip on hover and keyboard
  (arrow keys, Home/End, Esc), and a visually hidden data table ("View data" for screen readers).
-->
<template>
  <div class="area-chart">
    <div v-if="series.length > 1" class="area-chart__legend">
      <span v-for="s in series" :key="s.key" class="area-chart__legend-item">
        <span class="area-chart__key" :style="{ background: s.color }" />
        {{ s.label }}
      </span>
    </div>
    <div ref="box" class="area-chart__plot" :style="{ height: `${height}px` }">
      <svg
        v-if="width > 0"
        :width="width"
        :height="height"
        role="img"
        :aria-label="label"
        tabindex="0"
        @pointermove="onPointer"
        @pointerleave="active = null"
        @keydown="onKey"
        @blur="active = null"
      >
        <g class="area-chart__grid">
          <template v-for="tick in ticks" :key="tick">
            <line :x1="PAD.left" :x2="width - PAD.right" :y1="y(tick)" :y2="y(tick)" :class="{ baseline: tick === 0 }" />
            <text :x="PAD.left - 8" :y="y(tick) + 4" text-anchor="end">{{ format(tick) }}</text>
          </template>
          <template v-for="(ts, i) in timestamps" :key="ts">
            <text v-if="i % every === 0" :x="x(i)" :y="height - PAD.bottom + 18" text-anchor="middle">{{ axisTime(ts, stepSeconds) }}</text>
          </template>
        </g>
        <g>
          <path v-for="(s, k) in series" :key="`a-${s.key}`" :d="areaPath(k)" :fill="s.color" class="area-chart__area" />
          <path v-for="(s, k) in series" :key="`l-${s.key}`" :d="linePath(k)" :stroke="s.color" class="area-chart__line" />
        </g>
        <g v-if="active !== null">
          <line class="area-chart__crosshair" :x1="x(active)" :x2="x(active)" :y1="PAD.top" :y2="height - PAD.bottom" />
          <circle v-for="(s, k) in series" :key="`d-${s.key}`" :cx="x(active)" :cy="y(stacked[k]![active]![1])" r="4" :fill="s.color" class="area-chart__dot" />
        </g>
      </svg>
      <div v-if="empty" class="area-chart__empty text-muted">{{ emptyText }}</div>
      <div v-if="active !== null && !empty" class="area-chart__tooltip" :style="tooltipStyle" role="status" aria-live="polite">
        <div class="area-chart__tooltip-time">{{ absoluteTime(timestamps[active]!) }}</div>
        <div v-for="s in [...series].reverse()" :key="s.key" class="area-chart__tooltip-row">
          <span class="area-chart__key" :style="{ background: s.color }" />
          <span class="text-muted">{{ s.label }}</span>
          <strong>{{ format(s.values[active] ?? 0) }}</strong>
        </div>
        <div v-if="series.length > 1" class="area-chart__tooltip-row area-chart__tooltip-total">
          <span class="area-chart__key" />
          <span class="text-muted">{{ t('charts.total') }}</span>
          <strong>{{ format(series.reduce((a, s) => a + (s.values[active!] ?? 0), 0)) }}</strong>
        </div>
      </div>
    </div>
    <div class="visually-hidden">
    <table>
      <caption>{{ label }}</caption>
      <thead>
        <tr>
          <th scope="col">{{ t('charts.time') }}</th>
          <th v-for="s in series" :key="s.key" scope="col">{{ s.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(ts, i) in timestamps" :key="ts">
          <th scope="row">{{ absoluteTime(ts) }}</th>
          <td v-for="s in series" :key="s.key">{{ format(s.values[i] ?? 0) }}</td>
        </tr>
      </tbody>
    </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { t } from '@/i18n'
import { absoluteTime, axisTime } from '@/utils/format'

import { labelEvery, niceTicks, stack } from './scale'

export interface AreaSeries {
  key: string
  label: string
  color: string
  values: number[]
}

const props = withDefaults(
  defineProps<{
    timestamps: string[]
    series: AreaSeries[]
    stepSeconds: number
    label: string
    format: (value: number) => string
    height?: number
    emptyText?: string
  }>(),
  { height: 240, emptyText: 'No traffic in this period' },
)

const PAD = { left: 64, right: 12, top: 10, bottom: 28 }

const box = ref<HTMLDivElement | null>(null)
const width = ref(0)
const active = ref<number | null>(null)
let observer: ResizeObserver | null = null

const plotW = computed(() => Math.max(0, width.value - PAD.left - PAD.right))
const plotH = computed(() => Math.max(0, props.height - PAD.top - PAD.bottom))
const n = computed(() => props.timestamps.length)
const stacked = computed(() => stack(props.series.map((s) => s.values)))
const max = computed(() => Math.max(0, ...(stacked.value[stacked.value.length - 1] ?? []).map((p) => p[1])))
const empty = computed(() => max.value === 0)
const ticks = computed(() => niceTicks(max.value, props.height < 200 ? 3 : 4))
const top = computed(() => ticks.value[ticks.value.length - 1] || 1)
const every = computed(() => labelEvery(n.value, plotW.value, props.stepSeconds >= 3600 ? 96 : 72))

const x = (i: number) => PAD.left + (n.value > 1 ? (plotW.value * i) / (n.value - 1) : plotW.value / 2)
const y = (v: number) => PAD.top + plotH.value * (1 - v / top.value)

function linePath(k: number): string {
  return (stacked.value[k] ?? []).map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join('')
}

function areaPath(k: number): string {
  const pts = stacked.value[k] ?? []
  if (!pts.length) return ''
  const upper = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join('')
  const lower = pts.map((p, i) => `L${x(pts.length - 1 - i).toFixed(1)},${y(pts[pts.length - 1 - i]![0]).toFixed(1)}`).join('')
  return `${upper}${lower}Z`
}

function onPointer(event: PointerEvent): void {
  if (!n.value) return
  const rect = (event.currentTarget as SVGElement).getBoundingClientRect()
  const rel = (event.clientX - rect.left - PAD.left) / (plotW.value || 1)
  active.value = Math.min(n.value - 1, Math.max(0, Math.round(rel * (n.value - 1))))
}

function onKey(event: KeyboardEvent): void {
  const last = n.value - 1
  if (last < 0) return
  if (event.key === 'ArrowRight') active.value = Math.min(last, (active.value ?? -1) + 1)
  else if (event.key === 'ArrowLeft') active.value = Math.max(0, (active.value ?? last + 1) - 1)
  else if (event.key === 'Home') active.value = 0
  else if (event.key === 'End') active.value = last
  else if (event.key === 'Escape') active.value = null
  else return
  event.preventDefault()
}

const tooltipStyle = computed(() => {
  if (active.value === null) return {}
  const left = x(active.value)
  return left > width.value / 2 ? { right: `${width.value - left + 14}px`, top: `${PAD.top}px` } : { left: `${left + 14}px`, top: `${PAD.top}px` }
})

onMounted(() => {
  if (!box.value) return
  width.value = box.value.clientWidth
  observer = new ResizeObserver(([entry]) => {
    if (entry) width.value = entry.contentRect.width
  })
  observer.observe(box.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<style scoped>
.area-chart {
  position: relative;
  width: 100%;
}

.area-chart__legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  margin: 0 0 6px 64px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.area-chart__legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.area-chart__key {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 3px;
  flex: none;
}

.area-chart__plot {
  position: relative;
}

svg {
  display: block;
  overflow: visible;
  outline: none;
  touch-action: pan-y;
}

svg:focus-visible {
  outline: 2px solid var(--q-primary);
  outline-offset: 2px;
  border-radius: 4px;
}

.area-chart__grid line {
  stroke: var(--chart-grid);
  stroke-width: 1;
  shape-rendering: crispEdges;
}

.area-chart__grid line.baseline {
  stroke: var(--chart-axis);
}

text {
  fill: var(--text-tertiary);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.area-chart__area {
  opacity: 0.22;
}

.area-chart__line {
  fill: none;
  stroke-width: 2;
  stroke-linejoin: round;
}

.area-chart__crosshair {
  stroke: var(--chart-axis);
  stroke-width: 1;
  shape-rendering: crispEdges;
}

.area-chart__dot {
  stroke: var(--chart-surface);
  stroke-width: 2;
}

.area-chart__empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
}

.area-chart__tooltip {
  position: absolute;
  z-index: 2;
  min-width: 200px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--surface-raised);
  box-shadow: var(--shadow-pop);
  font-size: var(--fs-12);
  pointer-events: none;
}

.area-chart__tooltip-time {
  margin-bottom: 6px;
  color: var(--text-secondary);
}

.area-chart__tooltip-row {
  display: flex;
  align-items: center;
  gap: 6px;
  line-height: 1.7;
}

.area-chart__tooltip-row strong {
  margin-left: auto;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.area-chart__tooltip-total {
  border-top: 1px solid var(--border);
  margin-top: 4px;
  padding-top: 4px;
}
</style>
