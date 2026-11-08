<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Page title row with optional breadcrumbs and actions. -->
<template>
  <header class="page-header">
    <nav v-if="crumbs?.length" class="page-header__crumbs" :aria-label="t('common.breadcrumbs')">
      <template v-for="(crumb, i) in crumbs" :key="i">
        <router-link v-if="crumb.to" :to="crumb.to" class="text-link">{{ crumb.label }}</router-link>
        <span v-else>{{ crumb.label }}</span>
        <q-icon v-if="i < crumbs.length - 1" :name="iconChevronRight" size="14px" class="text-faint" />
      </template>
    </nav>
    <div class="page-header__row">
      <div v-if="$slots.icon" class="page-header__icon"><slot name="icon" /></div>
      <div class="page-header__titles">
        <h1 class="page-header__title"><slot name="title">{{ title }}</slot></h1>
        <div v-if="caption || $slots.caption" class="page-header__caption"><slot name="caption">{{ caption }}</slot></div>
      </div>
      <div v-if="$slots.actions" class="page-header__actions"><slot name="actions" /></div>
    </div>
  </header>
</template>

<script setup lang="ts">
import type { RouteLocationRaw } from 'vue-router'

import { t } from '@/i18n'
import { iconChevronRight } from '@/utils/icons'

withDefaults(defineProps<{ title?: string; caption?: string; crumbs?: { label: string; to?: RouteLocationRaw }[] }>(), {
  title: undefined,
  caption: undefined,
  crumbs: undefined,
})
</script>

<style scoped>
.page-header {
  margin-bottom: 20px;
}

.page-header__crumbs {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.page-header__row {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.page-header__icon {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: var(--surface);
  flex: none;
}

.page-header__titles {
  flex: 1;
  min-width: 200px;
}

.page-header__title {
  margin: 0;
  font-size: var(--fs-24);
  font-weight: 650;
  line-height: 1.2;
  letter-spacing: -0.015em;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.page-header__caption {
  margin-top: 4px;
  color: var(--text-secondary);
  font-size: var(--fs-14);
}

.page-header__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

@media (max-width: 599px) {
  .page-header__title {
    font-size: var(--fs-20);
  }
}
</style>
