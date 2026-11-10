<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- "Managed" label on an object the provisioning file declares (T-O09): read-only here, change the file instead. -->
<template>
  <span v-if="isManaged(item)" class="managed-badge" tabindex="0" :aria-label="t('provisioning.managedHelp')">
    <q-icon :name="iconLock" size="12px" />
    {{ t('provisioning.managed') }}
    <q-tooltip max-width="300px">{{ t('provisioning.managedHelp') }}</q-tooltip>
  </span>
</template>

<script setup lang="ts">
import { t } from '@/i18n'
import { iconLock } from '@/utils/icons'
import { isManaged, type Managed } from '@/utils/managed'

defineProps<{ item: Managed | null | undefined }>()
</script>

<style scoped>
.managed-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 20px;
  padding: 0 7px;
  border: 1px solid color-mix(in srgb, var(--q-primary) 45%, transparent);
  border-radius: 4px;
  font-size: 11px;
  font-weight: 650;
  letter-spacing: 0.04em;
  color: var(--q-primary);
  white-space: nowrap;
  vertical-align: middle;
}
</style>
