<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Card with a title row (title, caption, actions) and a body; `flush` removes body padding for tables. -->
<template>
  <section class="page-card q-card" :aria-labelledby="titleId">
    <header v-if="title || $slots.title || $slots.actions" class="page-card__head">
      <div class="page-card__titles">
        <h2 :id="titleId" class="page-card__title">
          <slot name="title">{{ title }}</slot>
        </h2>
        <div v-if="caption || $slots.caption" class="page-card__caption">
          <slot name="caption">{{ caption }}</slot>
        </div>
      </div>
      <div v-if="$slots.actions" class="page-card__actions"><slot name="actions" /></div>
    </header>
    <div class="page-card__body" :class="{ 'page-card__body--flush': flush }">
      <slot />
    </div>
    <footer v-if="$slots.footer" class="page-card__footer"><slot name="footer" /></footer>
  </section>
</template>

<script setup lang="ts">
import { useId } from 'vue'

withDefaults(defineProps<{ title?: string; caption?: string; flush?: boolean }>(), { title: undefined, caption: undefined, flush: false })
const titleId = useId()
</script>

<style scoped>
.page-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.page-card__head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px 10px;
  min-height: 52px;
}

.page-card__titles {
  flex: 1;
  min-width: 0;
}

.page-card__title {
  margin: 0;
  font-size: var(--fs-14);
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.page-card__caption {
  margin-top: 2px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.page-card__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.page-card__body {
  padding: 4px 16px 16px;
  flex: 1;
  min-width: 0;
}

.page-card__body--flush {
  padding: 0;
}

.page-card__footer {
  border-top: 1px solid var(--border);
  padding: 8px 16px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
