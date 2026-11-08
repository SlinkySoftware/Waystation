<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Enabled / disabled pill for sign-in providers (icon + label, never colour alone). -->
<template>
  <span class="state" :class="enabled ? 'state--on' : 'state--off'">
    <q-icon :name="enabled ? iconOk : iconBlocked" size="14px" />
    {{ enabled ? t('authProviders.enabled') : t('authProviders.disabled') }}
  </span>
</template>

<script setup lang="ts">
import { t } from '@/i18n'
import { iconBlocked, iconOk } from '@/utils/icons'

defineProps<{ enabled: boolean }>()
</script>

<style scoped>
.state {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: var(--fs-12);
  font-weight: 550;
}

.state--on {
  color: var(--status-ok);
  background: var(--status-ok-bg);
}

.state--off {
  color: var(--text-secondary);
  background: var(--surface-sunken);
}
</style>
