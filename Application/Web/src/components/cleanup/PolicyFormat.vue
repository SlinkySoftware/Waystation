<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A policy's format: the format mark and name, or a layers icon and "All formats" for `*`. -->
<template>
  <span class="policy-format">
    <FormatIcon v-if="format !== '*'" :mark="formatMark(format)" :size="iconOnly ? 22 : 18" brand :decorative="!iconOnly" />
    <q-icon v-else :name="iconLayers" :size="iconOnly ? '22px' : '18px'" class="policy-format__all" :aria-label="iconOnly ? label : undefined" />
    <span v-if="!iconOnly">{{ label }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { CleanupFormat } from '@/api/types'
import FormatIcon from '@/components/FormatIcon.vue'
import { formatMark } from '@/utils/domain'
import { iconLayers } from '@/utils/icons'

import { formatScope } from './cleanup'

const props = withDefaults(defineProps<{ format: CleanupFormat; iconOnly?: boolean }>(), { iconOnly: false })
const label = computed(() => formatScope(props.format))
</script>

<style scoped>
.policy-format {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

.policy-format__all {
  color: var(--text-secondary);
}
</style>
