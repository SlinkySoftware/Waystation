<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Stored bytes against a repository quota (SPECIFICATION §4.8): a thin meter, "used of quota", and a badge from
     80 % (warning) and at 100 % (exceeded). Compact for table cells; the full form adds the mode and what it means. -->
<template>
  <div class="quota" :class="[`quota--${meta.state}`, { 'quota--compact': compact }]">
    <div class="quota__line">
      <span class="quota__usage num">{{ compact ? meta.percent : meta.usage }}</span>
      <span v-if="!compact" class="quota__mode text-faint">{{ t('quota.title') }} · {{ quotaModeLabel(quota.mode) }}</span>
      <span v-if="meta.state !== 'ok' || !compact" class="ws-badge quota__badge" :class="meta.tone" data-test="quota-badge">
        <q-icon :name="meta.icon" />
        {{ meta.label }}
      </span>
    </div>
    <div
      class="quota__bar"
      role="meter"
      :aria-label="t('quota.aria')"
      aria-valuemin="0"
      :aria-valuemax="quota.quota_bytes"
      :aria-valuenow="quota.used_bytes"
      :aria-valuetext="meta.usage"
    >
      <span class="quota__fill" :style="{ width: `${meta.fill * 100}%` }" />
      <span class="quota__mark" aria-hidden="true" />
    </div>
    <div v-if="!compact" class="quota__text text-caption text-muted">{{ meta.description }} {{ t('quota.shared') }}</div>
    <q-tooltip v-if="compact">{{ meta.usage }} · {{ quotaModeLabel(quota.mode) }} · {{ meta.description }}</q-tooltip>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { RepositoryQuota } from '@/api/types'
import { t } from '@/i18n'
import { quotaMeta, quotaModeLabel } from '@/utils/quota'

const props = withDefaults(defineProps<{ quota: RepositoryQuota; compact?: boolean }>(), { compact: false })
const meta = computed(() => quotaMeta(props.quota))
</script>

<style scoped>
.quota {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.quota--compact {
  gap: 3px;
  min-width: 72px;
  align-items: flex-end;
}

.quota__line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 8px;
}

.quota__usage {
  font-weight: 600;
}

.quota--compact .quota__usage {
  font-weight: 400;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.quota__mode {
  font-size: var(--fs-12);
}

.quota__bar {
  position: relative;
  width: 100%;
  height: 6px;
  border-radius: 3px;
  background: var(--border);
  overflow: hidden;
}

.quota--compact .quota__bar {
  height: 4px;
}

.quota__fill {
  display: block;
  height: 100%;
  background: var(--status-ok);
  transition: width 200ms var(--ease-out);
}

.quota--warning .quota__fill {
  background: var(--status-degraded);
}

.quota--exceeded .quota__fill {
  background: var(--status-blocked);
}

/* The 80 % warning threshold. */
.quota__mark {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 80%;
  width: 1px;
  background: var(--text-secondary);
  opacity: 0.5;
}
</style>
