<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Repository / upstream status: icon + label + colour (never colour alone). -->
<template>
  <span class="ws-badge" :class="meta.tone">
    <q-icon :name="meta.icon" />
    {{ meta.label }}
    <q-tooltip v-if="tooltip">{{ meta.description }}</q-tooltip>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { RepositoryStatus } from '@/api/types'
import { statusMeta } from '@/utils/domain'

const props = withDefaults(defineProps<{ status: RepositoryStatus; tooltip?: boolean }>(), { tooltip: true })
const meta = computed(() => statusMeta(props.status))
</script>
