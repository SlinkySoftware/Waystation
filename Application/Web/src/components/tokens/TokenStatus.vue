<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Token status badge: icon + label + colour. -->
<template>
  <span class="ws-badge" :class="meta.tone"><q-icon :name="meta.icon" />{{ meta.label }}</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { ApiToken } from '@/api/types'

import { tokenStatusMeta } from './tokens'

const props = defineProps<{ status: ApiToken['status'] }>()
const meta = computed(() => tokenStatusMeta(props.status))
</script>
