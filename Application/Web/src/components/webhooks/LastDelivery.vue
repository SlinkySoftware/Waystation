<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A webhook's latest delivery in a table cell: status, the event, the result and when. -->
<template>
  <div v-if="delivery" class="last-delivery">
    <div class="last-delivery__row">
      <DeliveryStatusPill :status="delivery.status" :attempts="delivery.attempts" />
      <TimeCell :value="delivery.created_at" class="last-delivery__time" />
    </div>
    <div class="last-delivery__meta">{{ eventLabel(delivery.event) }} · {{ deliveryResult(delivery) }}</div>
  </div>
  <span v-else class="text-faint">{{ t('webhooks.never') }}</span>
</template>

<script setup lang="ts">
import type { WebhookDeliverySummary } from '@/api/types'
import TimeCell from '@/components/TimeCell.vue'
import { t } from '@/i18n'

import DeliveryStatusPill from './DeliveryStatusPill.vue'
import { deliveryResult, eventLabel } from './webhooks'

defineProps<{ delivery: WebhookDeliverySummary | null }>()
</script>

<style scoped>
.last-delivery {
  display: grid;
  gap: 4px;
}

.last-delivery__row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.last-delivery__time {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.last-delivery__meta {
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
