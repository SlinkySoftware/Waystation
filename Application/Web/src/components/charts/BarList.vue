<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Ranked horizontal bars: label, value and a bar scaled to the largest item. The values are text, so the bars are
     decorative and the list reads the same without them (no information by colour or length alone). -->
<template>
  <ol class="bar-list" :aria-label="label">
    <li v-for="item in items" :key="item.key" class="bar-list__item">
      <div class="bar-list__row">
        <span class="bar-list__label"><slot name="label" :item="item">{{ item.label }}</slot></span>
        <span class="bar-list__value">{{ format(item.value) }}</span>
        <span v-if="showShare" class="bar-list__share">{{ share(item.value) }}</span>
      </div>
      <div class="bar-list__track" aria-hidden="true">
        <div class="bar-list__bar" :style="{ width: `${width(item.value)}%`, background: item.color ?? 'var(--q-primary)' }" />
      </div>
    </li>
  </ol>
</template>

<script setup lang="ts">
import { computed } from 'vue'

export interface BarItem {
  key: string
  label: string
  value: number
  color?: string
}

const props = withDefaults(defineProps<{ items: BarItem[]; label: string; format: (v: number) => string; total?: number | null; showShare?: boolean }>(), {
  total: null,
  showShare: true,
})

const max = computed(() => Math.max(0, ...props.items.map((i) => i.value)))
const sum = computed(() => props.total ?? props.items.reduce((a, i) => a + Math.max(0, i.value), 0))
const width = (v: number) => (max.value > 0 ? Math.max(v > 0 ? 1.5 : 0, (Math.max(0, v) / max.value) * 100) : 0)
const share = (v: number) => (sum.value > 0 ? `${((Math.max(0, v) / sum.value) * 100).toFixed(v / sum.value < 0.1 ? 1 : 0)}%` : '–')
</script>

<style scoped>
.bar-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 10px;
}

.bar-list__row {
  display: grid;
  grid-template-columns: 1fr auto 48px;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-13);
}

.bar-list__label {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.bar-list__value {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.bar-list__share {
  text-align: right;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.bar-list__track {
  margin-top: 4px;
  height: 6px;
  border-radius: 999px;
  background: var(--chart-track);
  overflow: hidden;
}

.bar-list__bar {
  height: 100%;
  border-radius: 999px;
  transition: width 600ms var(--ease-out);
}

@media (prefers-reduced-motion: reduce) {
  .bar-list__bar {
    transition: none;
  }
}
</style>
