<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Product mark (UI_DESIGN §8): geometry from assets/brand/geometry.ts (shared with scripts/gen-brand.mjs), colours
     from the theme tokens. `mono` paints the marker in currentColor with the glyph cut out (any background shows
     through); below 20 px the 16 px-tuned glyph is used. -->
<template>
  <svg :width="size" :height="size" :viewBox="mark.viewBox" class="brand-mark" :class="`brand-mark--${variant}`" v-bind="a11y">
    <title v-if="label">{{ label }}</title>
    <template v-if="variant === 'mono'">
      <mask :id="maskId" maskUnits="userSpaceOnUse" x="0" y="0" width="32" height="32">
        <rect width="32" height="32" fill="#fff" />
        <path fill="#000" stroke="#000" :stroke-width="mark.slabStroke" stroke-linejoin="round" :d="mark.slab" />
        <path fill="none" stroke="#000" :stroke-width="mark.layerStroke" stroke-linecap="round" stroke-linejoin="round" :d="layers" />
      </mask>
      <path class="brand-mark__marker" :stroke-width="mark.markerStroke" stroke-linejoin="round" :d="mark.marker" :mask="`url(#${maskId})`" />
    </template>
    <template v-else>
      <path class="brand-mark__marker" :stroke-width="mark.markerStroke" stroke-linejoin="round" :d="mark.marker" />
      <path class="brand-mark__glyph" :stroke-width="mark.slabStroke" stroke-linejoin="round" :d="mark.slab" />
      <path class="brand-mark__glyph brand-mark__layers" :stroke-width="mark.layerStroke" stroke-linecap="round" stroke-linejoin="round" :d="layers" />
    </template>
  </svg>
</template>

<script setup lang="ts">
import { computed, useId } from 'vue'

import { MARK, MARK_SMALL } from '@/assets/brand/geometry'

const props = withDefaults(defineProps<{ size?: number; variant?: 'color' | 'mono'; label?: string }>(), {
  size: 28,
  variant: 'color',
  label: undefined,
})

const maskId = `ws-mark-${useId()}`
const mark = computed(() => (props.size < 20 ? MARK_SMALL : MARK))
const layers = computed(() => mark.value.layers.join(''))
// Decorative next to the product name; a labelled image when it stands alone.
const a11y = computed(() => (props.label ? { role: 'img', 'aria-label': props.label } : { 'aria-hidden': true as const }))
</script>

<style scoped>
.brand-mark {
  flex: none;
}

.brand-mark__marker {
  fill: var(--brand-500);
  stroke: var(--brand-500);
}

.brand-mark__glyph {
  fill: var(--text-on-brand);
  stroke: var(--text-on-brand);
}

.brand-mark__layers {
  fill: none;
}

.brand-mark--mono .brand-mark__marker {
  fill: currentColor;
  stroke: currentColor;
}
</style>
