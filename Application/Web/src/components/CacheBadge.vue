<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Cache state badge (SPEC §4.7 vocabulary): icon + label; COALESCED shows the follower count on the owner. -->
<template>
  <span class="ws-badge cache-badge" :class="meta.tone">
    <q-icon :name="meta.icon" />
    <span>{{ meta.label }}</span>
    <span v-if="followers" class="cache-badge__count" :aria-label="t('live.followers', { count: followers })">+{{ followers }}</span>
    <q-tooltip>{{ followers ? `${meta.description} ${t('live.followers', { count: followers })}` : meta.description }}</q-tooltip>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { CacheState } from '@/api/types'
import { t } from '@/i18n'
import { cacheMeta } from '@/utils/domain'

const props = withDefaults(defineProps<{ state: CacheState; followers?: number }>(), { followers: 0 })
const meta = computed(() => cacheMeta(props.state))
</script>

<style scoped>
.cache-badge {
  font-size: 11px;
  letter-spacing: 0.02em;
  height: 20px;
  padding: 0 7px;
}

.cache-badge__count {
  margin-left: 2px;
  padding-left: 4px;
  border-left: 1px solid currentColor;
  font-variant-numeric: tabular-nums;
}
</style>
