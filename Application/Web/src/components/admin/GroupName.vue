<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A group's name: local groups by name and description; mapped groups by the provider group they match and the
     provider's name. -->
<template>
  <div class="group-name">
    <q-icon :name="group.source === 'local' ? iconGroups : iconAuthProviders" size="20px" class="group-name__icon" />
    <div class="group-name__text">
      <div class="group-name__title" :class="{ mono: group.source !== 'local' }">{{ group.source === 'local' ? group.name : group.external_group }}</div>
      <div class="group-name__sub">{{ group.source === 'local' ? group.description : t('admin.detail.mappedHint', { provider: group.provider?.name ?? group.provider?.id ?? '' }) }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Group } from '@/api/types'
import { t } from '@/i18n'
import { iconAuthProviders, iconGroups } from '@/utils/icons'

defineProps<{ group: Group }>()
</script>

<style scoped>
.group-name {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.group-name__icon {
  color: var(--q-primary);
  flex: none;
}

.group-name__text {
  min-width: 0;
}

.group-name__title {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}

.group-name__sub {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}
</style>
