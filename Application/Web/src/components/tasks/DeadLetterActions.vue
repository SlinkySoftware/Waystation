<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Retry (primary) and discard for one dead letter; retry is unavailable for malformed entries. -->
<template>
  <span class="dead-actions">
    <q-btn v-if="letter.retryable" outline dense no-caps color="primary" :icon="iconRetry" :label="t('tasks.dead.retry')" :loading="busy" class="dead-actions__retry" @click="emit('retry')" />
    <q-btn flat round dense :icon="iconDelete" :disable="busy" :aria-label="t('tasks.dead.discard')" @click="emit('discard')">
      <q-tooltip>{{ t('tasks.dead.discard') }}</q-tooltip>
    </q-btn>
  </span>
</template>

<script setup lang="ts">
import type { DeadLetter } from '@/api/types'
import { t } from '@/i18n'
import { iconDelete, iconRetry } from '@/utils/icons'

defineProps<{ letter: DeadLetter; busy: boolean }>()
const emit = defineEmits<{ retry: []; discard: [] }>()
</script>

<style scoped>
.dead-actions {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.dead-actions__retry {
  min-height: 32px;
  padding: 0 10px;
}
</style>
