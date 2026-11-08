<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A policy's criteria as compact chips: selecting criteria filled, protections outlined with a shield. -->
<template>
  <span class="criteria-chips">
    <span v-for="chip in chips" :key="chip.key" class="criteria-chip" :class="{ 'criteria-chip--protects': chip.protects }">
      <q-icon v-if="chip.protects" :name="iconLock" size="12px" />
      <span class="criteria-chip__text">{{ chip.label }}</span>
    </span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { CleanupCriteria } from '@/api/types'
import { iconLock } from '@/utils/icons'

import { criteriaChips } from './cleanup'

const props = defineProps<{ criteria: CleanupCriteria }>()
const chips = computed(() => criteriaChips(props.criteria))
</script>

<style scoped>
.criteria-chips {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  max-width: 100%;
}

.criteria-chip {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  max-width: 220px;
  height: 22px;
  padding: 0 8px;
  border-radius: 6px;
  background: var(--surface-brand);
  color: var(--text-primary);
  font-size: var(--fs-12);
  font-weight: 550;
  white-space: nowrap;
}

.criteria-chip--protects {
  background: transparent;
  box-shadow: inset 0 0 0 1px var(--border);
  color: var(--text-secondary);
}

.criteria-chip__text {
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
