<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Low-disk banner (SPECIFICATION §4.8): while free space is below the low-disk guard, new cache fills and uploads
     are refused. Polls GET /storage/guard (every minute, and at once on a `storage` activity event); dismissible for
     the browser session, per episode (it comes back when the disk runs low again). -->
<template>
  <div v-if="visible && guard" class="low-disk" role="alert" data-test="low-disk-banner">
    <q-icon :name="iconWarning" size="18px" class="low-disk__icon" />
    <span class="low-disk__text">{{ t('lowDisk.banner', { free: formatBytes(guard.free_bytes), floor: formatBytes(guard.floor_bytes) }) }}</span>
    <q-btn flat dense no-caps :label="t('lowDisk.open')" :to="{ name: 'storage' }" class="low-disk__action" />
    <q-btn flat dense round size="sm" :icon="iconClose" :aria-label="t('lowDisk.dismiss')" @click="dismiss" />
  </div>
</template>

<script setup lang="ts">
import { computed, onScopeDispose, ref } from 'vue'

import { api, call } from '@/api/client'
import type { StorageGuard } from '@/api/types'
import { usePolling } from '@/composables/usePolling'
import { t } from '@/i18n'
import { useLiveStore } from '@/stores/live'
import { formatBytes } from '@/utils/format'
import { iconClose, iconWarning } from '@/utils/icons'

const props = withDefaults(defineProps<{ intervalMs?: number }>(), { intervalMs: 60_000 })

const DISMISS_KEY = 'ws.lowDiskDismissed'
const guard = ref<StorageGuard | null>(null)
const dismissed = ref(readDismissed())

/** One low-disk episode: when it started (the worker's report), else the floor it is measured against. */
const episode = computed(() => (guard.value ? (guard.value.low_since ?? `floor:${guard.value.floor_bytes}`) : ''))
const visible = computed(() => Boolean(guard.value?.low) && dismissed.value !== episode.value)

async function refresh(): Promise<void> {
  try {
    guard.value = await call(api.GET('/storage/guard'))
  } catch {
    // Not signed in yet, or the server is away: keep the last state.
  }
}

function dismiss(): void {
  dismissed.value = episode.value
  try {
    window.sessionStorage.setItem(DISMISS_KEY, episode.value)
  } catch {
    // Storage blocked: dismissed for this page only.
  }
}

function readDismissed(): string | null {
  try {
    return window.sessionStorage.getItem(DISMISS_KEY)
  } catch {
    return null
  }
}

void refresh()
usePolling(() => true, refresh, props.intervalMs)
const off = useLiveStore().onActivity((event) => {
  if (event.kind === 'storage') void refresh()
})
onScopeDispose(off)

defineExpose({ refresh })
</script>

<style scoped>
.low-disk {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 16px;
  background: var(--status-degraded-bg);
  color: var(--text-primary);
  border-top: 1px solid var(--border);
  font-size: var(--fs-13);
}

.low-disk__icon {
  color: var(--status-degraded);
  flex: none;
}

.low-disk__text {
  flex: 1;
  min-width: 0;
}

.low-disk__action {
  color: var(--status-degraded);
  font-weight: 600;
}
</style>
