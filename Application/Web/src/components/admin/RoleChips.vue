<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Role ids as named chips (description in a tooltip); after `max`, a "+N" chip lists the rest. -->
<template>
  <span class="role-chips">
    <span v-if="!roles.length" class="text-faint">{{ empty ?? t('admin.roles.none') }}</span>
    <span v-for="id in shown" :key="id" class="role-chip" :class="{ 'role-chip--admin': isAdmin(id) }">
      {{ directory.roleName(id) }}
      <q-tooltip v-if="directory.roleById.get(id)?.description">{{ directory.roleById.get(id)?.description }}</q-tooltip>
    </span>
    <span v-if="rest.length" class="role-chip role-chip--more">
      {{ t('admin.roles.more', { count: rest.length }) }}
      <q-tooltip>{{ rest.map((id) => directory.roleName(id)).join(', ') }}</q-tooltip>
    </span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { t } from '@/i18n'
import { useDirectoryStore } from '@/stores/directory'

const props = withDefaults(defineProps<{ roles: string[]; max?: number; empty?: string }>(), { max: 3, empty: undefined })
const directory = useDirectoryStore()
const shown = computed(() => props.roles.slice(0, props.max))
const rest = computed(() => props.roles.slice(props.max))
const isAdmin = (id: string) => (directory.roleById.get(id)?.permissions ?? []).some((p) => p.action === 'admin')
</script>

<style scoped>
.role-chips {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
}

.role-chip {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  border-radius: 999px;
  background: var(--surface-sunken);
  border: 1px solid var(--border);
  font-size: var(--fs-12);
  font-weight: 550;
  white-space: nowrap;
}

.role-chip--admin {
  color: var(--status-degraded);
  border-color: color-mix(in srgb, var(--status-degraded) 40%, transparent);
  background: var(--status-degraded-bg);
}

.role-chip--more {
  color: var(--text-secondary);
}
</style>
