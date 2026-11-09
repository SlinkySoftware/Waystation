<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A policy's last run in one cell: status, when, and what it removed (or its progress while running). -->
<template>
  <div v-if="run" class="last-run">
    <div class="last-run__head">
      <RunStatusPill :status="run.status" />
      <TimeCell :value="run.started_at" class="last-run__when" />
    </div>
    <RunDetail v-if="run.status === 'running'" :run="run" />
    <div v-else-if="run.status === 'succeeded'" class="last-run__bytes">
      {{ runOutcome(run) }}
    </div>
    <div v-else-if="run.error" class="last-run__error">{{ run.error }}<q-tooltip max-width="360px">{{ run.error }}</q-tooltip></div>
  </div>
  <span v-else class="text-faint">{{ t('cleanup.neverRun') }}</span>
</template>

<script setup lang="ts">
import type { CleanupRunSummary } from '@/api/types'
import TimeCell from '@/components/TimeCell.vue'
import { t } from '@/i18n'

import { runOutcome } from './cleanup'
import RunDetail from './RunDetail.vue'
import RunStatusPill from './RunStatusPill.vue'

defineProps<{ run: CleanupRunSummary | null }>()
</script>

<style scoped>
.last-run {
  display: grid;
  gap: 4px;
  min-width: 140px;
}

.last-run__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.last-run__when,
.last-run__bytes {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.last-run__bytes {
  font-variant-numeric: tabular-nums;
}

.last-run__error {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-12);
  color: var(--status-blocked);
}
</style>
