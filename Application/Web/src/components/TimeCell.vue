<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Relative time with the absolute time (user's locale and timezone) in a tooltip. -->
<template>
  <time v-if="value" class="time-cell" :datetime="value">
    {{ relative }}
    <q-tooltip>{{ absolute }}</q-tooltip>
  </time>
  <span v-else class="text-faint">{{ placeholder }}</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { useNow } from '@/composables/useNow'
import { absoluteTime, relativeTime } from '@/utils/format'

const props = withDefaults(defineProps<{ value: string | null | undefined; placeholder?: string }>(), { placeholder: '–' })
const now = useNow()
const relative = computed(() => relativeTime(props.value, now.value))
const absolute = computed(() => absoluteTime(props.value))
</script>

<style scoped>
.time-cell {
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
</style>
