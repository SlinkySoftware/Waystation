<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A QR code drawn in the browser as one SVG path (utils/qrcode): the value never leaves the page. Always dark on
     white with a quiet zone, whatever the theme, so phone cameras read it. -->
<template>
  <svg class="qr" :viewBox="`0 0 ${dimension} ${dimension}`" :width="size" :height="size" role="img" :aria-label="label" shape-rendering="crispEdges">
    <rect :width="dimension" :height="dimension" fill="#ffffff" />
    <path :d="path" fill="#000000" />
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { encodeQr, qrPath } from '@/utils/qrcode'

const props = withDefaults(defineProps<{ value: string; label: string; size?: number }>(), { size: 200 })
const MARGIN = 4
const matrix = computed(() => encodeQr(props.value))
const dimension = computed(() => matrix.value.size + MARGIN * 2)
const path = computed(() => qrPath(matrix.value, MARGIN))
</script>

<style scoped>
.qr {
  display: block;
  border-radius: var(--radius-sm);
}
</style>
