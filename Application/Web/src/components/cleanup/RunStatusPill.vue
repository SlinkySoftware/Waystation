<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Status of a cleanup or GC run: icon + label + colour (never colour alone); the icon turns while running. -->
<template>
  <span class="ws-badge" :class="[meta.tone, { 'run-pill--running': status === 'running' }]">
    <q-icon :name="meta.icon" />
    {{ meta.label }}
    <q-tooltip v-if="tooltip">{{ meta.description }}</q-tooltip>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { type RunStatus, runStatusMeta } from './cleanup'

const props = withDefaults(defineProps<{ status: RunStatus; tooltip?: boolean }>(), { tooltip: true })
const meta = computed(() => runStatusMeta(props.status))
</script>

<style scoped>
.run-pill--running .q-icon {
  animation: run-pill-spin 1.4s linear infinite;
}

@keyframes run-pill-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .run-pill--running .q-icon {
    animation: none;
  }
}
</style>
