<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Pulsing dot + active transfer count; shows the connection state when the stream is down. -->
<template>
  <button type="button" class="live" :class="`live--${state}`" :aria-label="label" @click="emit('click')">
    <span class="live__dot" aria-hidden="true" />
    <span class="live__count">{{ state === 'open' ? live.activeCount : '' }}</span>
    <span class="live__text">{{ text }}</span>
    <q-tooltip>{{ label }}</q-tooltip>
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { t } from '@/i18n'
import { useLiveStore } from '@/stores/live'

const emit = defineEmits<{ click: [] }>()
const live = useLiveStore()

const state = computed(() => (live.status === 'open' ? 'open' : live.status === 'idle' ? 'idle' : 'connecting'))
const text = computed(() => (state.value === 'open' ? t('live.active') : state.value === 'connecting' ? t('live.connecting') : t('live.offline')))
const label = computed(() => (state.value === 'open' ? t('live.activeCount', { count: live.activeCount }) : text.value))
</script>

<style scoped>
.live {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 32px;
  padding: 0 12px 0 10px;
  margin-right: 4px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text-primary);
  font: inherit;
  font-size: var(--fs-13);
  cursor: pointer;
}

.live:hover {
  border-color: var(--border-strong);
}

.live__dot {
  position: relative;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--status-offline);
}

.live--open .live__dot {
  background: var(--status-ok);
}

.live--open .live__dot::after {
  content: '';
  position: absolute;
  inset: -4px;
  border-radius: 50%;
  border: 2px solid var(--status-ok);
  opacity: 0;
  animation: pulse 1.8s var(--ease-out) infinite;
}

.live--connecting .live__dot {
  background: var(--status-degraded);
}

.live__count {
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  min-width: 1ch;
}

.live__text {
  color: var(--text-secondary);
}

@media (max-width: 599px) {
  .live__text {
    display: none;
  }
}

@keyframes pulse {
  0% {
    transform: scale(0.6);
    opacity: 0.8;
  }
  100% {
    transform: scale(1.6);
    opacity: 0;
  }
}
</style>
