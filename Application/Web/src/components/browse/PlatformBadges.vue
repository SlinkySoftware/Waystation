<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<template>
  <span class="platforms">
    <span v-for="p in shown" :key="key(p)" class="platform mono">{{ label(p) }}</span>
    <span v-if="rest > 0" class="platform platform--more">+{{ rest }}<q-tooltip>{{ platforms.slice(max).map(label).join(', ') }}</q-tooltip></span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { Platform } from '@/api/types'

const props = withDefaults(defineProps<{ platforms: Platform[]; max?: number }>(), { max: 6 })
const shown = computed(() => props.platforms.slice(0, props.max))
const rest = computed(() => props.platforms.length - props.max)
const key = (p: Platform) => `${p.os}/${p.architecture}/${p.variant ?? ''}`
const label = (p: Platform) => `${p.architecture}${p.variant ? `/${p.variant}` : ''}`
</script>

<style scoped>
.platforms {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
}

.platform {
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--surface-subtle);
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
}

.platform--more {
  font-family: var(--font-sans);
}
</style>
