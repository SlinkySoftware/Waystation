<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- The actions on one user: open, edit, reset password (local accounts), unlock (when locked), sign out everywhere,
     disable/enable and delete (never on yourself). -->
<template>
  <q-btn flat round dense size="sm" :icon="iconMore" :aria-label="t('admin.users.actions', { name: user.username })" @click.stop>
    <q-menu auto-close anchor="bottom right" self="top right">
      <q-list dense class="user-menu">
        <q-item clickable :to="{ name: 'admin-user', params: { id: user.id } }">
          <q-item-section side><q-icon :name="iconPerson" size="18px" /></q-item-section>
          <q-item-section>{{ t('admin.users.open') }}</q-item-section>
        </q-item>
        <q-item clickable @click="emit('edit')">
          <q-item-section side><q-icon :name="iconEdit" size="18px" /></q-item-section>
          <q-item-section>{{ t('admin.users.edit') }}</q-item-section>
        </q-item>
        <q-item v-if="user.source === 'local'" clickable @click="emit('reset')">
          <q-item-section side><q-icon :name="iconPassword" size="18px" /></q-item-section>
          <q-item-section>{{ t('admin.users.resetPassword') }}</q-item-section>
        </q-item>
        <q-item v-if="user.locked" clickable @click="actions.unlock(user)">
          <q-item-section side><q-icon :name="iconUnlock" size="18px" /></q-item-section>
          <q-item-section>{{ t('admin.users.unlock') }}</q-item-section>
        </q-item>
        <q-item clickable @click="actions.signOut(user)">
          <q-item-section side><q-icon :name="iconLogout" size="18px" /></q-item-section>
          <q-item-section>{{ t('admin.users.signOut') }}</q-item-section>
        </q-item>
        <template v-if="!self">
          <q-separator />
          <q-item clickable @click="actions.toggleDisabled(user)">
            <q-item-section side><q-icon :name="user.disabled ? iconPersonOn : iconPersonOff" size="18px" /></q-item-section>
            <q-item-section>{{ user.disabled ? t('admin.users.enable') : t('admin.users.disable') }}</q-item-section>
          </q-item>
          <q-item clickable class="text-negative" @click="actions.remove(user, () => emit('deleted'))">
            <q-item-section side><q-icon :name="iconDelete" size="18px" color="negative" /></q-item-section>
            <q-item-section>{{ t('admin.users.delete') }}</q-item-section>
          </q-item>
        </template>
      </q-list>
    </q-menu>
  </q-btn>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { UserDetail, UserSummary } from '@/api/types'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { iconDelete, iconEdit, iconLogout, iconMore, iconPassword, iconPerson, iconPersonOff, iconPersonOn, iconUnlock } from '@/utils/icons'

import { useUserActions } from './useAdminActions'

const props = defineProps<{ user: UserSummary | UserDetail }>()
const emit = defineEmits<{ edit: []; reset: []; changed: [user: UserDetail | null]; deleted: [] }>()
const auth = useAuthStore()
const self = computed(() => auth.user?.id === props.user.id)
const actions = useUserActions((u) => emit('changed', u))
</script>

<style scoped>
.user-menu {
  min-width: 220px;
}
</style>
