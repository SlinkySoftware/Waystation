<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Empty state: illustration, one sentence, the primary action (UI_DESIGN §3). -->
<template>
  <div class="empty-state" :class="{ 'empty-state--compact': compact }" role="status">
    <div class="empty-state__art" aria-hidden="true">
      <q-icon :name="icon" />
    </div>
    <div class="empty-state__title">{{ title }}</div>
    <div v-if="text" class="empty-state__text">{{ text }}</div>
    <div v-if="$slots.default" class="empty-state__actions"><slot /></div>
  </div>
</template>

<script setup lang="ts">
import { iconPackage } from '@/utils/icons'

withDefaults(defineProps<{ title: string; text?: string; icon?: string; compact?: boolean }>(), {
  text: undefined,
  icon: iconPackage,
  compact: false,
})
</script>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 48px 16px;
  gap: 6px;
}

.empty-state--compact {
  padding: 24px 12px;
}

.empty-state__art {
  display: grid;
  place-items: center;
  width: 64px;
  height: 64px;
  margin-bottom: 8px;
  border-radius: 18px;
  background: radial-gradient(circle at 30% 25%, var(--brand-100), var(--surface-brand));
  color: var(--q-primary);
  font-size: 32px;
}

.empty-state--compact .empty-state__art {
  width: 44px;
  height: 44px;
  font-size: 22px;
  border-radius: 12px;
}

.empty-state__title {
  font-weight: 600;
  font-size: var(--fs-16);
}

.empty-state__text {
  max-width: 420px;
  color: var(--text-secondary);
}

.empty-state__actions {
  margin-top: 12px;
  display: flex;
  gap: 8px;
}
</style>
