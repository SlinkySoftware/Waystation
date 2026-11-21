<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Theme and density (UI_DESIGN §2, §4.11): saved to the account (and this browser, for the first paint). -->
<template>
  <PageCard :title="t('ui.profile.appearance')" :caption="t('ui.profile.appearanceCaption')">
    <div class="appearance">
      <div class="appearance__row">
        <span :id="themeId" class="appearance__label">{{ t('shell.theme') }}</span>
        <q-btn-toggle :model-value="theme.mode" no-caps unelevated toggle-color="primary" class="appearance__toggle" :aria-labelledby="themeId" :options="themeOptions" @update:model-value="(mode: ThemeMode) => theme.save({ theme: mode })" />
      </div>
      <div class="appearance__row">
        <span :id="densityId" class="appearance__label">{{ t('ui.density.label') }}</span>
        <q-btn-toggle :model-value="theme.density" no-caps unelevated toggle-color="primary" class="appearance__toggle" :aria-labelledby="densityId" :options="densityOptions" @update:model-value="(density: Density) => theme.save({ density })" />
        <span class="appearance__hint">{{ t('ui.density.hint') }}</span>
      </div>
    </div>
  </PageCard>
</template>

<script setup lang="ts">
import { computed, useId } from 'vue'

import PageCard from '@/components/PageCard.vue'
import { t } from '@/i18n'
import { type Density, type ThemeMode, useThemeStore } from '@/stores/theme'
import { iconDark, iconLight, iconSystemTheme } from '@/utils/icons'

const theme = useThemeStore()
const themeId = useId()
const densityId = useId()
const themeOptions = computed(() => [
  { label: t('theme.light'), value: 'light', icon: iconLight },
  { label: t('theme.dark'), value: 'dark', icon: iconDark },
  { label: t('theme.system'), value: 'system', icon: iconSystemTheme },
])
const densityOptions = computed(() => [
  { label: t('ui.density.comfortable'), value: 'comfortable' },
  { label: t('ui.density.compact'), value: 'compact' },
])
</script>

<style scoped>
.appearance {
  display: grid;
  gap: 14px;
}

.appearance__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
}

.appearance__label {
  min-width: 80px;
  font-weight: 550;
}

.appearance__toggle {
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
}

.appearance__toggle :deep(.q-btn) {
  padding: 4px 14px;
}

.appearance__hint {
  font-size: var(--fs-13);
  color: var(--text-secondary);
}
</style>
