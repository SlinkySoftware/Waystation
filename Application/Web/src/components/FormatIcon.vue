<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A format or distribution mark from the bundled SVG sprite. Monochrome by default, brand colour when `brand`. -->
<template>
  <svg
    class="format-icon"
    :class="{ 'format-icon--brand': brand }"
    :width="size"
    :height="size"
    :style="brand ? { '--mark-color': colour } : undefined"
    :role="decorative ? undefined : 'img'"
    :aria-hidden="decorative ? 'true' : undefined"
    :aria-label="decorative ? undefined : title"
    focusable="false"
  >
    <title v-if="!decorative">{{ title }}</title>
    <use :href="`${sprite}#${mark}`" />
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import sprite from '@/assets/formats.svg?url'

import { type BrandMark, brandMarks } from './brandMarks'

const props = withDefaults(defineProps<{ mark: BrandMark; size?: number; brand?: boolean; decorative?: boolean }>(), {
  size: 20,
  brand: false,
  decorative: false,
})

const title = computed(() => brandMarks[props.mark]?.title ?? props.mark)
const colour = computed(() => {
  const hex = brandMarks[props.mark]?.hex ?? ''
  // Black/near-black marks (AlmaLinux, Rust) would vanish in dark mode: use the text colour instead.
  return /^#0{6}$/i.test(hex) ? 'currentColor' : hex
})
</script>

<style scoped>
.format-icon {
  display: inline-block;
  flex: none;
  fill: currentColor;
  color: var(--text-secondary);
  vertical-align: middle;
  transition: color var(--dur) var(--ease-out);
}

.format-icon--brand {
  color: var(--mark-color);
}
</style>
