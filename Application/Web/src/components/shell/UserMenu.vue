<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<template>
  <q-btn flat round dense class="user-btn" :aria-label="t('shell.userMenu')">
    <span class="user-btn__avatar">{{ initials }}</span>
    <q-menu anchor="bottom right" self="top right" :offset="[0, 8]">
      <div class="user-menu">
        <div class="user-menu__who">
          <div class="user-menu__name">{{ auth.user?.display_name }}</div>
          <div class="user-menu__meta">{{ auth.user?.username }} · {{ auth.user?.source }}</div>
        </div>
        <q-separator />
        <q-list dense padding>
          <q-item v-close-popup clickable :to="{ name: 'profile' }">
            <q-item-section avatar><q-icon :name="iconProfile" /></q-item-section>
            <q-item-section>{{ t('shell.profile') }}</q-item-section>
          </q-item>
          <q-item v-close-popup clickable :to="{ name: 'profile', hash: '#tokens' }">
            <q-item-section avatar><q-icon :name="iconToken" /></q-item-section>
            <q-item-section>{{ t('shell.myTokens') }}</q-item-section>
          </q-item>
          <q-item-label header class="user-menu__label">{{ t('shell.theme') }}</q-item-label>
          <div class="user-menu__themes">
            <q-btn-toggle
              :model-value="theme.mode"
              spread
              no-caps
              unelevated
              dense
              toggle-color="primary"
              :options="themeOptions"
              @update:model-value="(mode: ThemeMode) => theme.save({ theme: mode })"
            />
          </div>
          <q-separator spaced />
          <q-item v-close-popup clickable @click="signOut">
            <q-item-section avatar><q-icon :name="iconLogout" /></q-item-section>
            <q-item-section>{{ t('shell.signOut') }}</q-item-section>
          </q-item>
        </q-list>
      </div>
    </q-menu>
  </q-btn>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useLiveStore } from '@/stores/live'
import { type ThemeMode, useThemeStore } from '@/stores/theme'
import { iconLogout, iconProfile, iconToken } from '@/utils/icons'

const auth = useAuthStore()
const theme = useThemeStore()
const router = useRouter()

const initials = computed(() => {
  const name = auth.user?.display_name || auth.user?.username || '?'
  return name
    .split(/\s+/)
    .map((p) => p[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
})

const themeOptions = computed(() => [
  { label: t('theme.light'), value: 'light' },
  { label: t('theme.dark'), value: 'dark' },
  { label: t('theme.system'), value: 'system' },
])

async function signOut(): Promise<void> {
  await auth.logout().catch(() => undefined)
  useLiveStore().reset()
  await router.replace({ name: 'sign-in' })
}
</script>

<style scoped>
.user-btn__avatar {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  background: var(--q-primary);
  color: var(--text-on-brand);
  font-size: 12px;
  font-weight: 650;
}

.user-menu {
  min-width: 260px;
}

.user-menu__who {
  padding: 12px 16px;
}

.user-menu__name {
  font-weight: 600;
}

.user-menu__meta {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.user-menu__label {
  padding-bottom: 6px;
}

.user-menu__themes {
  padding: 0 12px 4px;
}
</style>
