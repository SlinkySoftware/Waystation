<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A job's log: monospace lines (text, never HTML), error lines tinted, a "Live" badge while the run is going,
     "Follow" to keep the newest line in view, and copy. -->
<template>
  <section class="log-viewer">
    <div class="log-viewer__head">
      <h3 class="log-viewer__h">
        {{ t('tasks.run.log') }}
        <span v-if="live" class="ws-badge tone-hit log-viewer__live"><span class="log-viewer__dot" />{{ t('tasks.run.logLive') }}</span>
      </h3>
      <q-space />
      <q-toggle v-model="follow" dense :label="t('tasks.run.follow')"><q-tooltip>{{ t('tasks.run.followHelp') }}</q-tooltip></q-toggle>
      <q-btn flat round dense :icon="iconCopy" :disable="!lines.length" :aria-label="t('tasks.run.copyLog')" @click="copy(lines.join('\n'), t('tasks.run.log'))">
        <q-tooltip>{{ t('tasks.run.copyLog') }}</q-tooltip>
      </q-btn>
    </div>
    <div ref="box" class="log-viewer__box mono" role="log" aria-live="off" tabindex="0">
      <div v-for="(line, i) in lines" :key="i" class="log-viewer__line" :class="{ 'log-viewer__line--error': ERROR_LINE.test(line) }">{{ line }}</div>
      <div v-if="!lines.length" class="text-faint">{{ t('tasks.run.logEmpty') }}</div>
    </div>
    <div class="log-viewer__note">{{ t('tasks.run.lines', { count: lines.length, n: lines.length }) }}</div>
  </section>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

import { useCopy } from '@/composables/useNotify'
import { t } from '@/i18n'
import { iconCopy } from '@/utils/icons'

const props = defineProps<{ lines: string[]; live: boolean }>()
const ERROR_LINE = /\b(ERROR|Traceback)\b/
const copy = useCopy()
const follow = ref(true)
const box = ref<HTMLElement | null>(null)

async function scrollToEnd(): Promise<void> {
  if (!follow.value) return
  await nextTick()
  if (box.value) box.value.scrollTop = box.value.scrollHeight
}

watch(() => [props.lines.length, follow.value], () => void scrollToEnd(), { immediate: true })
</script>

<style scoped>
.log-viewer {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 240px;
  margin-top: 24px;
}

.log-viewer__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.log-viewer__h {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 8px;
  font-size: var(--fs-14);
  font-weight: 600;
  line-height: 1.4;
}

.log-viewer__live {
  gap: 6px;
}

.log-viewer__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  animation: log-viewer-pulse 1.6s ease-in-out infinite;
}

@keyframes log-viewer-pulse {
  50% {
    opacity: 0.3;
  }
}

@media (prefers-reduced-motion: reduce) {
  .log-viewer__dot {
    animation: none;
  }
}

.log-viewer__box {
  flex: 1;
  min-height: 200px;
  max-height: 60vh;
  overflow: auto;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
  font-size: var(--fs-12);
  line-height: 1.6;
}

.log-viewer__line {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.log-viewer__line--error {
  color: var(--status-blocked);
}

.log-viewer__note {
  margin-top: 6px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
