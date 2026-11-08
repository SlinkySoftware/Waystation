<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Right-hand drawer with every live transfer (opened from the top-bar live indicator). -->
<template>
  <div class="live-drawer">
    <header class="live-drawer__head">
      <div>
        <div class="live-drawer__title">{{ t('live.title') }}</div>
        <div class="text-caption">{{ t('live.drawerCaption') }}</div>
      </div>
      <q-space />
      <q-btn flat round dense :icon="iconClose" :aria-label="t('common.close')" @click="emit('close')" />
    </header>
    <LiveTransfers :max-rows="200" compact />
  </div>
</template>

<script setup lang="ts">
import LiveTransfers from '@/components/live/LiveTransfers.vue'
import { t } from '@/i18n'
import { iconClose } from '@/utils/icons'

const emit = defineEmits<{ close: [] }>()
</script>

<style scoped>
.live-drawer__head {
  display: flex;
  align-items: center;
  padding: 16px;
}

.live-drawer__title {
  font-size: var(--fs-16);
  font-weight: 650;
}
</style>
