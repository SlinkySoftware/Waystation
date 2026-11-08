<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A value (URL, digest, command) in monospace with a copy button. -->
<template>
  <div class="copy-field" :class="{ 'copy-field--compact': compact }">
    <span class="copy-field__value mono" :title="value">{{ value }}</span>
    <q-btn flat dense round size="sm" :icon="iconCopy" :aria-label="t('common.copyWhat', { what: label ?? value })" @click="copy(value, label)">
      <q-tooltip>{{ t('common.copy') }}</q-tooltip>
    </q-btn>
  </div>
</template>

<script setup lang="ts">
import { useCopy } from '@/composables/useNotify'
import { t } from '@/i18n'
import { iconCopy } from '@/utils/icons'

withDefaults(defineProps<{ value: string; label?: string; compact?: boolean }>(), { label: undefined, compact: false })
const copy = useCopy()
</script>

<style scoped>
.copy-field {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  min-width: 0;
  padding: 2px 2px 2px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
}

.copy-field--compact {
  border: 0;
  background: transparent;
  padding-left: 0;
}

.copy-field__value {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  min-width: 0;
  color: var(--text-primary);
}
</style>
