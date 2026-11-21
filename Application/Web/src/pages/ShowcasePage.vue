<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Dev-only component showcase (T-U01): the gallery once in the light and once in the dark theme, side by side
     (stacked on narrow screens). The route exists only in dev and mock builds (router/routes.ts). -->
<template>
  <q-page class="ws-page showcase-page">
    <PageHeader :title="t('ui.showcase.title')" :caption="t('ui.showcase.caption')" />
    <div class="showcase">
      <section v-for="theme in THEMES" :key="theme" :class="['showcase__pane', `ws-theme-${theme}`]" :aria-label="t(`ui.showcase.${theme}`)" :data-theme="theme">
        <h2 class="showcase__theme">{{ t(`ui.showcase.${theme}`) }}</h2>
        <ShowcaseGallery />
      </section>
    </div>
  </q-page>
</template>

<script setup lang="ts">
import ShowcaseGallery from '@/components/dev/ShowcaseGallery.vue'
import PageHeader from '@/components/PageHeader.vue'
import { t } from '@/i18n'

const THEMES = ['light', 'dark'] as const
</script>

<style scoped>
.showcase-page {
  max-width: none;
}

.showcase {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.showcase__pane {
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--page-bg);
  color: var(--text-primary);
  min-width: 0;
}

.showcase__theme {
  margin: 0 0 12px;
  font-size: var(--fs-16);
  font-weight: 650;
}

@media (max-width: 1279px) {
  .showcase {
    grid-template-columns: 1fr;
  }
}
</style>
