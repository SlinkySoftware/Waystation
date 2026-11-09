<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A user's state as a badge: disabled, locked out, waiting for a password change or a first provider sign-in, or
     active. Icon + label, never colour alone. -->
<template>
  <span class="ws-badge" :class="meta.tone">
    <q-icon :name="meta.icon" />
    {{ meta.label }}
    <q-tooltip v-if="meta.help">{{ meta.help }}</q-tooltip>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { UserDetail, UserSummary } from '@/api/types'
import { t } from '@/i18n'
import { absoluteTime } from '@/utils/format'
import { iconBlocked, iconLock, iconOk, iconPassword, iconPending } from '@/utils/icons'

const props = defineProps<{ user: UserSummary | UserDetail }>()

const meta = computed(() => {
  const u = props.user
  if (u.disabled) return { label: t('admin.status.disabled'), icon: iconBlocked, tone: 'tone-blocked', help: '' }
  if (u.locked) {
    const until = 'locked_until' in u && u.locked_until ? t('admin.status.lockedUntil', { time: absoluteTime(u.locked_until) }) : ''
    return { label: t('admin.status.locked'), icon: iconLock, tone: 'tone-degraded', help: until }
  }
  if ('must_change_password' in u && u.must_change_password) return { label: t('admin.status.mustChange'), icon: iconPassword, tone: 'tone-offline', help: '' }
  if ('has_password' in u && u.source === 'local' && !u.has_password && !u.last_login) return { label: t('admin.status.invited'), icon: iconPending, tone: 'tone-offline', help: t('admin.status.invitedHelp') }
  return { label: t('admin.status.active'), icon: iconOk, tone: 'tone-ok', help: '' }
})
</script>
