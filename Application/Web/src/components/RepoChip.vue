<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Repository reference: format mark + name, linking to the repository page. -->
<template>
  <component :is="link ? RouterLink : 'span'" :to="link ? { name: 'repository', params: { name } } : undefined" class="repo-chip" :class="{ 'repo-chip--link': link }">
    <FormatIcon :mark="mark" :size="14" decorative />
    <span class="repo-chip__name">{{ name }}</span>
  </component>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import type { Format } from '@/api/types'
import { formatMark } from '@/utils/domain'

import type { BrandMark } from './brandMarks'
import FormatIcon from './FormatIcon.vue'

const props = withDefaults(defineProps<{ name: string; format?: Format | null; markOverride?: BrandMark; link?: boolean }>(), {
  format: null,
  markOverride: undefined,
  link: true,
})
const mark = computed<BrandMark>(() => props.markOverride ?? (props.format ? formatMark(props.format) : 'raw'))
</script>

<style scoped>
.repo-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 100%;
  height: 22px;
  padding: 0 8px 0 6px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-subtle);
  color: var(--text-primary);
  font-size: var(--fs-12);
  font-weight: 550;
  text-decoration: none;
  white-space: nowrap;
}

.repo-chip__name {
  overflow: hidden;
  text-overflow: ellipsis;
}

.repo-chip--link:hover,
.repo-chip--link:focus-visible {
  border-color: var(--q-primary);
  color: var(--q-primary);
}
</style>
