<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Pass/fail list of provider checks (connect, tls, bind, search...). -->
<template>
  <ul class="checks">
    <li v-for="(c, i) in checks" :key="`${c.name}-${i}`">
      <q-icon :name="c.ok ? iconCheck : iconClose" size="14px" :class="c.ok ? 'text-positive' : 'text-negative'" :aria-label="c.ok ? 'ok' : 'failed'" />
      <span class="mono checks__name">{{ c.name }}</span>
      <span class="text-muted">{{ c.message }}</span>
    </li>
  </ul>
</template>

<script setup lang="ts">
import type { ProviderCheck } from '@/api/types'
import { iconCheck, iconClose } from '@/utils/icons'

defineProps<{ checks: ProviderCheck[] }>()
</script>

<style scoped>
.checks {
  list-style: none;
  margin: 4px 0 0 22px;
  padding: 0;
  display: grid;
  gap: 2px;
  color: var(--text-primary);
}

.checks li {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: var(--fs-12);
}

.checks__name {
  min-width: 84px;
}
</style>
