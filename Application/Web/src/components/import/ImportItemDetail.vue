<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- One Nexus repository's field-level differences (Nexus value → what Waystation uses, and why) and notes. -->
<template>
  <div class="nx-detail" :class="{ 'nx-detail--compact': compact }">
    <table v-if="item.differences.length" class="nx-detail__diffs">
      <thead>
        <tr>
          <th scope="col">{{ t('nexusImport.diff.field') }}</th>
          <th scope="col">{{ t('nexusImport.diff.nexus') }}</th>
          <th scope="col">{{ t('nexusImport.diff.waystation') }}</th>
          <th scope="col">{{ t('nexusImport.diff.note') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(d, i) in item.differences" :key="i">
          <td class="mono">{{ d.field }}</td>
          <td class="mono">{{ d.nexus ?? '—' }}</td>
          <td class="mono">{{ d.waystation ?? '—' }}</td>
          <td>{{ d.note }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else-if="!item.notes.length" class="nx-detail__none">{{ t('nexusImport.noDifferences') }}</p>
    <ul v-if="item.notes.length" class="nx-detail__notes">
      <li v-for="(n, i) in item.notes" :key="i"><q-icon :name="iconInfo" size="14px" />{{ n }}</li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import type { NexusImportItem } from '@/api/types'
import { t } from '@/i18n'
import { iconInfo } from '@/utils/icons'

withDefaults(defineProps<{ item: NexusImportItem; compact?: boolean }>(), { compact: false })
</script>

<style scoped>
.nx-detail {
  padding: 4px 0 8px 40px;
  overflow-x: auto;
}
.nx-detail--compact {
  padding: 0;
}
.nx-detail__diffs {
  border-collapse: collapse;
  width: 100%;
  font-size: var(--fs-13);
}
.nx-detail__diffs th {
  text-align: left;
  font-weight: 600;
  color: var(--text-secondary);
  padding: 4px 12px 4px 0;
  border-bottom: 1px solid var(--border);
}
.nx-detail__diffs td {
  padding: 4px 12px 4px 0;
  vertical-align: top;
  white-space: normal;
  border-bottom: 1px solid var(--border);
}
.nx-detail__diffs td.mono {
  word-break: break-all;
  max-width: 260px;
}
.nx-detail__none {
  margin: 0;
  color: var(--text-secondary);
}
.nx-detail__notes {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  color: var(--text-secondary);
  font-size: var(--fs-13);
}
.nx-detail__notes li {
  display: flex;
  gap: 6px;
  align-items: flex-start;
}
</style>
