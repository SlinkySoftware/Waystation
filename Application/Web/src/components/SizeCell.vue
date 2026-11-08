<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A size in binary units with the exact byte count in a tooltip. -->
<template>
  <span v-if="bytes !== null && bytes !== undefined" class="size-cell">
    {{ formatBytes(bytes) }}
    <q-tooltip>{{ exact }}</q-tooltip>
  </span>
  <span v-else class="text-faint">–</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { t } from '@/i18n'
import { formatBytes } from '@/utils/format'

const props = defineProps<{ bytes: number | null | undefined }>()
const exact = computed(() => t('common.bytesExact', { bytes: new Intl.NumberFormat().format(props.bytes ?? 0) }))
</script>

<style scoped>
.size-cell {
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
</style>
