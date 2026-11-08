<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Tiny trend line with a soft fill; decorative next to a labelled number (the KPI carries the value). -->
<template>
  <svg class="sparkline" :viewBox="`0 0 ${W} ${height}`" preserveAspectRatio="none" :height="height" aria-hidden="true">
    <path v-if="area" :d="area" class="sparkline__area" :style="{ fill: color }" />
    <path v-if="line" :d="line" class="sparkline__line" :style="{ stroke: color }" vector-effect="non-scaling-stroke" />
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{ values: (number | null)[]; height?: number; color?: string; min?: number | null }>(), {
  height: 32,
  color: 'var(--q-primary)',
  min: 0,
})

const W = 100
const PAD = 2

const points = computed(() => {
  const vals = props.values.map((v) => (v === null || !Number.isFinite(v) ? null : v))
  const nums = vals.filter((v): v is number => v !== null)
  if (nums.length < 2) return []
  const lo = props.min ?? Math.min(...nums)
  const hi = Math.max(...nums)
  const span = hi - lo || 1
  const step = W / (vals.length - 1)
  return vals.map((v, i) => (v === null ? null : [i * step, PAD + (props.height - PAD * 2) * (1 - (v - lo) / span)] as const))
})

const line = computed(() => {
  let d = ''
  let pen = false
  for (const p of points.value) {
    if (!p) {
      pen = false
      continue
    }
    d += `${pen ? 'L' : 'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`
    pen = true
  }
  return d
})

const area = computed(() => {
  const pts = points.value.filter((p): p is readonly [number, number] => p !== null)
  if (pts.length < 2) return ''
  return `M${pts[0]![0]},${props.height}` + pts.map((p) => `L${p[0].toFixed(2)},${p[1].toFixed(2)}`).join('') + `L${pts[pts.length - 1]![0]},${props.height}Z`
})
</script>

<style scoped>
.sparkline {
  display: block;
  width: 100%;
  overflow: visible;
}

.sparkline__line {
  fill: none;
  stroke-width: 1.5;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.sparkline__area {
  opacity: 0.12;
}
</style>
