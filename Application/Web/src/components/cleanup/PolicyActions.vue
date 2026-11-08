<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Row actions for a policy: run now and history as labelled icon buttons, edit and delete in a menu (touch targets
     ≥ 40 px). -->
<template>
  <span class="policy-actions">
    <q-btn v-if="canEdit" flat round dense :icon="iconPlay" :disable="running" :aria-label="t('cleanup.action.runWhat', { name: policy.name })" @click="emit('run')">
      <q-tooltip>{{ t('cleanup.action.run') }}</q-tooltip>
    </q-btn>
    <q-btn flat round dense :icon="iconHistory" :aria-label="t('cleanup.action.historyWhat', { name: policy.name })" @click="emit('history')">
      <q-tooltip>{{ t('cleanup.action.history') }}</q-tooltip>
    </q-btn>
    <q-btn v-if="canEdit" flat round dense :icon="iconMore" :aria-label="t('cleanup.action.moreWhat', { name: policy.name })">
      <q-menu anchor="bottom right" self="top right">
        <q-list dense class="policy-actions__menu">
          <q-item v-close-popup clickable :aria-label="t('cleanup.action.editWhat', { name: policy.name })" @click="emit('edit')">
            <q-item-section avatar><q-icon :name="iconEdit" size="18px" /></q-item-section>
            <q-item-section>{{ t('cleanup.action.edit') }}</q-item-section>
          </q-item>
          <q-item v-close-popup clickable class="text-negative" :aria-label="t('cleanup.action.deleteWhat', { name: policy.name })" @click="emit('delete')">
            <q-item-section avatar><q-icon :name="iconDelete" size="18px" /></q-item-section>
            <q-item-section>{{ t('cleanup.action.delete') }}</q-item-section>
          </q-item>
        </q-list>
      </q-menu>
    </q-btn>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { CleanupPolicy } from '@/api/types'
import { t } from '@/i18n'
import { iconDelete, iconEdit, iconHistory, iconMore, iconPlay } from '@/utils/icons'

const props = defineProps<{ policy: CleanupPolicy; canEdit: boolean }>()
const emit = defineEmits<{ run: []; history: []; edit: []; delete: [] }>()
const running = computed(() => props.policy.last_run?.status === 'running')
</script>

<style scoped>
.policy-actions {
  display: inline-flex;
  gap: 2px;
}

.policy-actions .q-btn {
  min-width: 40px;
  min-height: 40px;
}

.policy-actions__menu {
  min-width: 140px;
}
</style>
