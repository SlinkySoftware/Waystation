<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Row actions for a webhook: send a test event and the delivery log as labelled icon buttons, edit and delete in a
     menu (touch targets ≥ 40 px). -->
<template>
  <span class="hook-actions">
    <q-btn flat round dense :icon="iconSend" :loading="testing" :aria-label="t('webhooks.action.testWhat', { name: webhook.name })" @click="emit('test')">
      <q-tooltip>{{ t('webhooks.action.test') }}</q-tooltip>
    </q-btn>
    <q-btn flat round dense :icon="iconHistory" :aria-label="t('webhooks.action.deliveriesWhat', { name: webhook.name })" @click="emit('deliveries')">
      <q-tooltip>{{ t('webhooks.action.deliveries') }}</q-tooltip>
    </q-btn>
    <q-btn flat round dense :icon="iconMore" :aria-label="t('webhooks.action.more', { name: webhook.name })">
      <q-menu anchor="bottom right" self="top right">
        <q-list dense class="hook-actions__menu">
          <q-item v-if="managed" class="text-faint">
            <q-item-section avatar><q-icon :name="iconLock" size="18px" /></q-item-section>
            <q-item-section>{{ t('provisioning.managedAction') }}</q-item-section>
          </q-item>
          <q-item v-close-popup clickable :disable="managed" :aria-label="t('webhooks.action.editWhat', { name: webhook.name })" @click="emit('edit')">
            <q-item-section avatar><q-icon :name="iconEdit" size="18px" /></q-item-section>
            <q-item-section>{{ t('webhooks.action.edit') }}</q-item-section>
          </q-item>
          <q-item v-close-popup clickable :disable="managed" class="text-negative" :aria-label="t('webhooks.action.deleteWhat', { name: webhook.name })" @click="emit('delete')">
            <q-item-section avatar><q-icon :name="iconDelete" size="18px" /></q-item-section>
            <q-item-section>{{ t('webhooks.action.delete') }}</q-item-section>
          </q-item>
        </q-list>
      </q-menu>
    </q-btn>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { Webhook } from '@/api/types'
import { t } from '@/i18n'
import { iconDelete, iconEdit, iconHistory, iconLock, iconMore, iconSend } from '@/utils/icons'
import { isManaged } from '@/utils/managed'

const props = defineProps<{ webhook: Webhook; testing: boolean }>()
const emit = defineEmits<{ test: []; deliveries: []; edit: []; delete: [] }>()
const managed = computed(() => isManaged(props.webhook))
</script>

<style scoped>
.hook-actions {
  display: inline-flex;
  gap: 2px;
}

.hook-actions .q-btn {
  min-width: 40px;
  min-height: 40px;
}

.hook-actions__menu {
  min-width: 140px;
}
</style>
