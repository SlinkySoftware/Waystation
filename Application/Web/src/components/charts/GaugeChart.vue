<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Radial gauge (UI_DESIGN §7 "gauge"): a 240° arc filled to value / max, the value as text in the middle and a
     caption under it. Exposed as a `meter` with a value text, so the reading never depends on the arc; the tone
     (ok / warning / critical) only recolours the arc, the text says the same. -->
<template>
  <div
    class="gauge"
    :class="`gauge--${tone}`"
    role="meter"
    :aria-label="label"
    :aria-valuenow="clamped"
    aria-valuemin="0"
    :aria-valuemax="max"
    :aria-valuetext="valueText ?? undefined"
    :style="{ width: `${size}px` }"
  >
    <svg :viewBox="`0 0 ${S} ${S * 0.82}`" :width="size" :height="size * 0.82" aria-hidden="true" focusable="false">
      <path :d="track" class="gauge__track" :stroke-width="stroke" />
      <path v-if="fraction > 0" :d="fill" class="gauge__fill" :stroke-width="stroke" />
      <line v-for="m in markLines" :key="m.key" :x1="m.x1" :y1="m.y1" :x2="m.x2" :y2="m.y2" class="gauge__mark" />
    </svg>
    <div class="gauge__center">
      <div class="gauge__value">{{ display }}</div>
      <div v-if="caption" class="gauge__caption">{{ caption }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

export type GaugeTone = 'ok' | 'warning' | 'critical'

const props = withDefaults(
  defineProps<{
    value: number
    max: number
    /** Accessible name, e.g. "Docker Hub pulls left". */
    label: string
    /** Big text in the middle (default: the value). */
    display?: string | null
    /** Small text under the value, e.g. "of 200". */
    caption?: string | null
    /** Spoken value, e.g. "142 of 200 pulls left". */
    valueText?: string | null
    /** Fractions of `max` drawn as ticks on the arc (e.g. a quota's 80 % mark). */
    marks?: number[]
    /** Arc colour; callers pick it from their own thresholds and say the state in text too. */
    tone?: GaugeTone
    size?: number
  }>(),
  { display: null, caption: null, valueText: null, marks: () => [], tone: 'ok', size: 132 },
)

const S = 100
const R = 40
const CX = 50
const CY = 50
const SWEEP = 240
const START = 90 + (360 - SWEEP) / 2 // degrees, clockwise from +x (SVG y points down)
const stroke = 9

const clamped = computed(() => Math.min(Math.max(0, props.value), Math.max(0, props.max)))
const fraction = computed(() => (props.max > 0 ? clamped.value / props.max : 0))
const display = computed(() => props.display ?? String(Math.round(clamped.value)))

function point(angle: number, radius = R): [number, number] {
  const rad = (angle * Math.PI) / 180
  return [CX + radius * Math.cos(rad), CY + radius * Math.sin(rad)]
}

function arc(from: number, to: number): string {
  const [x1, y1] = point(from)
  const [x2, y2] = point(to)
  const large = to - from > 180 ? 1 : 0
  return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${R} ${R} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`
}

const track = arc(START, START + SWEEP)
const fill = computed(() => arc(START, START + SWEEP * Math.max(0.004, fraction.value)))
const markLines = computed(() =>
  props.marks
    .filter((m) => m > 0 && m < 1)
    .map((m) => {
      const angle = START + SWEEP * m
      const [x1, y1] = point(angle, R - stroke / 2 - 1)
      const [x2, y2] = point(angle, R + stroke / 2 + 1)
      return { key: m, x1, y1, x2, y2 }
    }),
)
</script>

<style scoped>
.gauge {
  position: relative;
  display: inline-block;
  flex: none;
}

.gauge svg {
  display: block;
}

.gauge__track {
  fill: none;
  stroke: var(--chart-track);
  stroke-linecap: round;
}

.gauge__fill {
  fill: none;
  stroke: var(--chart-1);
  stroke-linecap: round;
  transition: d 600ms var(--ease-out);
}

.gauge--warning .gauge__fill {
  stroke: var(--status-degraded);
}

.gauge--critical .gauge__fill {
  stroke: var(--status-blocked);
}

.gauge__mark {
  stroke: var(--text-tertiary);
  stroke-width: 1.5;
}

.gauge__center {
  position: absolute;
  inset: 0 0 12% 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  pointer-events: none;
}

.gauge__value {
  font-size: var(--fs-20);
  font-weight: 650;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}

.gauge__caption {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

@media (prefers-reduced-motion: reduce) {
  .gauge__fill {
    transition: none;
  }
}
</style>
