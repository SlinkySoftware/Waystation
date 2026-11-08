<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Interim product mark (waypoint marker holding a package) until the brand task (T-U02). -->
<template>
  <svg :width="size" :height="size" viewBox="0 0 32 32" aria-hidden="true" class="brand-mark">
    <path class="brand-mark__pin" d="M16 1.5C9.6 1.5 4.5 6.6 4.5 13c0 8.3 9.6 16.4 10.6 17.3a1.4 1.4 0 0 0 1.8 0C17.9 29.4 27.5 21.3 27.5 13 27.5 6.6 22.4 1.5 16 1.5Z" />
    <path class="brand-mark__box" d="m16 6.8 6.4 3.4v6.9L16 20.5l-6.4-3.4v-6.9Zm-4.4 4.6v4.5l3.4 1.8v-4.5Zm8.8 0-3.4 1.8v4.5l3.4-1.8Zm-4.4-2.3-3.2 1.7 3.2 1.7 3.2-1.7Z" />
  </svg>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ size?: number }>(), { size: 28 })
</script>

<style scoped>
.brand-mark__pin {
  fill: var(--brand-500);
}

.brand-mark__box {
  fill: #fff;
}

:global(body.body--dark) .brand-mark__box {
  fill: #10201e;
}
</style>
