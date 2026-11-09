<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Read-only settings: label, value and the environment variable that sets it. -->
<template>
  <dl class="limits">
    <div v-for="i in items" :key="i.key" class="limits__row">
      <dt>
        {{ i.label }}
        <span v-if="i.env" class="limits__env mono">{{ i.env }}</span>
      </dt>
      <dd class="num">{{ i.value }}</dd>
    </div>
  </dl>
</template>

<script setup lang="ts">
import type { LimitItem } from './forms'

defineProps<{ items: LimitItem[] }>()
</script>

<style scoped>
.limits {
  margin: 0;
}

.limits__row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
}

.limits__row:last-child {
  border-bottom: 0;
}

.limits dt {
  display: grid;
  font-size: var(--fs-13);
}

.limits__env {
  font-size: 11px;
  color: var(--text-tertiary);
}

.limits dd {
  margin: 0;
  font-weight: 600;
  white-space: nowrap;
}
</style>
