<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A user as initials, display name and username (and "You" for the signed-in account). -->
<template>
  <div class="user-cell">
    <span class="user-cell__avatar" :class="{ 'user-cell__avatar--off': user.disabled }" aria-hidden="true">{{ initials }}</span>
    <div class="user-cell__text">
      <div class="user-cell__name">
        {{ user.display_name || user.username }}
        <span v-if="self" class="user-cell__self">{{ t('admin.users.self') }}</span>
      </div>
      <div class="user-cell__username mono">{{ user.username }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { UserDetail, UserSummary } from '@/api/types'
import { t } from '@/i18n'

const props = withDefaults(defineProps<{ user: UserSummary | UserDetail; self?: boolean }>(), { self: false })

const initials = computed(() => {
  const words = (props.user.display_name || props.user.username).split(/[\s._@-]+/).filter(Boolean)
  return ((words[0]?.[0] ?? '') + (words.length > 1 ? (words.at(-1)?.[0] ?? '') : (words[0]?.[1] ?? ''))).toUpperCase()
})
</script>

<style scoped>
.user-cell {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.user-cell__avatar {
  flex: none;
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--brand-500) 16%, transparent);
  color: var(--brand-600);
  font-size: var(--fs-12);
  font-weight: 700;
  letter-spacing: 0.02em;
}

body.body--dark .user-cell__avatar {
  color: var(--brand-400);
}

.user-cell__avatar--off {
  background: var(--surface-sunken);
  color: var(--text-tertiary);
}

.user-cell__text {
  min-width: 0;
}

.user-cell__name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.user-cell__self {
  padding: 0 6px;
  border-radius: 999px;
  background: var(--surface-sunken);
  border: 1px solid var(--border);
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
}

.user-cell__username {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
