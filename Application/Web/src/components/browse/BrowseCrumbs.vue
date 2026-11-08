<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- In-browser breadcrumbs (images › nginx › 1.29.3); each crumb sets the query state. -->
<template>
  <nav class="bcrumbs" :aria-label="t('browse.location')">
    <template v-for="(c, i) in crumbs" :key="i">
      <button v-if="i < crumbs.length - 1" type="button" class="bcrumbs__link" @click="emit('go', i)">
        <q-icon v-if="c.icon" :name="c.icon" size="16px" />{{ c.label }}
      </button>
      <span v-else class="bcrumbs__current" :class="{ mono: c.mono }" aria-current="location"><q-icon v-if="c.icon" :name="c.icon" size="16px" />{{ c.label }}</span>
      <q-icon v-if="i < crumbs.length - 1" :name="iconChevronRight" size="16px" class="text-faint" />
    </template>
  </nav>
</template>

<script setup lang="ts">
import { t } from '@/i18n'
import { iconChevronRight } from '@/utils/icons'

defineProps<{ crumbs: { label: string; icon?: string; mono?: boolean }[] }>()
const emit = defineEmits<{ go: [index: number] }>()
</script>

<style scoped>
.bcrumbs {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  font-size: var(--fs-14);
}

.bcrumbs__link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  border: 0;
  border-radius: 6px;
  background: none;
  color: var(--link);
  font: inherit;
  cursor: pointer;
}

.bcrumbs__link:hover {
  background: var(--hover-bg);
}

.bcrumbs__current {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  font-weight: 600;
}
</style>
