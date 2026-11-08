<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- The detail cell of a cleanup or GC run: a progress bar with its message while running, the error when it
     failed, nothing otherwise. -->
<template>
  <div v-if="run.status === 'running'" class="run-detail">
    <q-linear-progress :value="ratio ?? 0" :indeterminate="ratio === null" rounded size="6px" color="primary" class="run-detail__bar" :aria-label="run.progress.message ?? undefined" />
    <div class="run-detail__message">
      {{ run.progress.message }}
      <span v-if="ratio !== null" class="num">· {{ formatPercent(ratio, 0) }}</span>
    </div>
  </div>
  <div v-else-if="run.error" class="run-detail__error">{{ run.error }}</div>
  <span v-else class="text-faint">–</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { CleanupRunSummary, GcRun } from '@/api/types'
import { formatPercent } from '@/utils/format'

import { progressRatio } from './cleanup'

const props = defineProps<{ run: Pick<CleanupRunSummary | GcRun, 'status' | 'progress' | 'error'> }>()
const ratio = computed(() => progressRatio(props.run.progress))
</script>

<style scoped>
.run-detail {
  display: grid;
  gap: 4px;
  min-width: 140px;
}

.run-detail__bar :deep(.q-linear-progress__model) {
  transition: transform 300ms ease-out;
}

.run-detail__message {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.run-detail__error {
  font-size: var(--fs-12);
  color: var(--status-blocked);
}
</style>
