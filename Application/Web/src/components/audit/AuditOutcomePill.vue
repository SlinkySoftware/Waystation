<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Audit outcome: icon + label + colour (never colour alone); the failure reason in a tooltip or inline. -->
<template>
  <span class="audit-outcome">
    <span class="ws-badge" :class="outcome === 'success' ? 'tone-ok' : 'tone-error'">
      <q-icon :name="outcome === 'success' ? iconOk : iconError" />
      {{ t(`audit.outcomes.${outcome}`) }}
    </span>
    <span v-if="reason && inline" class="audit-outcome__reason">{{ auditReasonLabel(reason) }}</span>
    <q-tooltip v-else-if="reason">{{ auditReasonLabel(reason) }}</q-tooltip>
  </span>
</template>

<script setup lang="ts">
import type { AuditOutcome } from '@/api/types'
import { t } from '@/i18n'
import { auditReasonLabel } from '@/utils/audit'
import { iconError, iconOk } from '@/utils/icons'

withDefaults(defineProps<{ outcome: AuditOutcome; reason?: string | null; inline?: boolean }>(), { reason: null, inline: false })
</script>

<style scoped>
.audit-outcome {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.audit-outcome__reason {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
