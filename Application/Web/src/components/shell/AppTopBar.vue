<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Top bar: menu toggle, product mark + instance name, search (opens the command palette), live indicator, theme, user. -->
<template>
  <div class="topbar">
    <q-btn flat round dense :icon="iconMenu" :aria-label="t('nav.toggle')" class="topbar__menu" @click="emit('toggle-rail')" />
    <router-link to="/" class="topbar__brand" :aria-label="t('nav.home')">
      <BrandMark :size="26" />
      <span class="topbar__name">{{ config.config?.instance_name ?? 'Waystation' }}</span>
    </router-link>
    <span v-if="mock" class="topbar__mock">
      {{ t('shell.mockData') }}
      <q-tooltip>{{ t('shell.mockHelp') }}</q-tooltip>
    </span>

    <button type="button" class="topbar__search" :aria-label="t('shell.search')" aria-haspopup="dialog" :aria-keyshortcuts="keyshortcut" @click="emit('open-palette')">
      <q-icon :name="iconSearch" size="18px" />
      <span class="topbar__search-text">{{ t('shell.searchPlaceholder') }}</span>
      <kbd>{{ shortcut }}</kbd>
    </button>
    <q-btn flat round dense :icon="iconSearch" :aria-label="t('shell.search')" class="topbar__search-icon" @click="emit('open-palette')" />
    <q-space />

    <LiveIndicator @click="emit('open-live')" />

    <q-btn flat round dense :icon="themeIcon" :aria-label="themeLabel" @click="theme.cycle()">
      <q-tooltip>{{ themeLabel }}</q-tooltip>
    </q-btn>
    <q-btn flat round dense :icon="iconHelp" :aria-label="t('shell.help')" class="gt-xs">
      <q-tooltip>{{ t('shell.help') }}</q-tooltip>
    </q-btn>
    <UserMenu />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import BrandMark from '@/components/BrandMark.vue'
import { t } from '@/i18n'
import { useConfigStore } from '@/stores/config'
import { useThemeStore } from '@/stores/theme'
import { iconDark, iconHelp, iconLight, iconMenu, iconSearch, iconSystemTheme } from '@/utils/icons'

import LiveIndicator from './LiveIndicator.vue'
import UserMenu from './UserMenu.vue'

defineProps<{ railOpen: boolean }>()
const emit = defineEmits<{ 'toggle-rail': []; 'open-live': []; 'open-palette': [] }>()

const config = useConfigStore()
const theme = useThemeStore()
const mock = import.meta.env.WAYSTATION_MOCK === '1'
const mac = /Mac|iPhone|iPad/.test(navigator.platform)
const shortcut = mac ? '⌘K' : 'Ctrl K'
const keyshortcut = mac ? 'Meta+K' : 'Control+K'

const themeIcon = computed(() => (theme.mode === 'light' ? iconLight : theme.mode === 'dark' ? iconDark : iconSystemTheme))
const themeLabel = computed(() => t('shell.themeIs', { mode: t(`theme.${theme.mode}`) }))
</script>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 56px;
  padding: 0 12px;
}

.topbar__brand {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 10px 0 4px;
  color: var(--text-primary);
  text-decoration: none;
}

.topbar__name {
  font-weight: 650;
  font-size: 16px;
  letter-spacing: -0.01em;
}

.topbar__mock {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  color: var(--status-degraded);
  background: var(--status-degraded-bg);
  white-space: nowrap;
}

.topbar__search {
  display: flex;
  align-items: center;
  gap: 8px;
  width: min(420px, 34vw);
  height: 36px;
  margin-left: 16px;
  padding: 0 8px 0 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-subtle);
  color: var(--text-tertiary);
  font: inherit;
  cursor: pointer;
  text-align: left;
}

.topbar__search:hover {
  border-color: var(--border-strong);
}

.topbar__search-text {
  flex: 1;
}

kbd {
  font-family: var(--font-sans);
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-secondary);
}

.topbar__search-icon {
  display: none;
}

@media (max-width: 1023px) {
  .topbar__search {
    display: none;
  }

  .topbar__search-icon {
    display: inline-flex;
  }
}

@media (max-width: 599px) {
  .topbar__name,
  .topbar__mock {
    display: none;
  }
}
</style>
