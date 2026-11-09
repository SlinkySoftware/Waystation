<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Status of a webhook delivery: icon + label + colour (never colour alone). A pending delivery that already failed
     once reads "Retrying". -->
<template>
  <span class="ws-badge" :class="meta.tone">
    <q-icon :name="meta.icon" />
    {{ meta.label }}
    <q-tooltip v-if="tooltip">{{ meta.description }}</q-tooltip>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { WebhookDeliveryStatus } from '@/api/types'

import { deliveryMeta } from './webhooks'

const props = withDefaults(defineProps<{ status: WebhookDeliveryStatus; attempts?: number; tooltip?: boolean }>(), { attempts: 0, tooltip: true })
const meta = computed(() => deliveryMeta(props.status, props.attempts))
</script>
