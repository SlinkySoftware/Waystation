<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Live impact of a draft policy: "Would remove N items · X GiB across M repositories", per-repository bars and a
     way into the full candidate list. Fed by useCleanupPreview (debounced `limit: 0` previews). -->
<template>
  <aside class="impact" :class="{ 'impact--busy': loading }" aria-live="polite">
    <div class="impact__head">
      <div class="impact__title">{{ t('cleanup.impact.title') }}</div>
      <q-spinner v-if="loading" size="16px" color="primary" />
    </div>
    <div class="impact__scope">{{ scope }}</div>

    <div v-if="!ready" class="impact__hint">{{ t('cleanup.impact.needCriteria') }}</div>
    <div v-else-if="error && !totals" class="impact__hint impact__hint--error">{{ hasFieldErrors ? t('cleanup.impact.fixErrors') : errorMessage(error) }}</div>
    <div v-else-if="!totals" class="impact__hint">{{ t('cleanup.impact.checking') }}</div>
    <template v-else>
      <div class="impact__numbers">
        <div class="impact__count">{{ t('cleanup.impact.count', { count: totals.count, n: formatCount(totals.count) }) }}</div>
        <div class="impact__bytes"><SizeCell :bytes="totals.bytes" /> <span class="impact__across">{{ t('cleanup.impact.across', { count: affected.length }) }}</span></div>
      </div>
      <div v-if="totals.count === 0" class="impact__hint">{{ t('cleanup.impact.nothing') }}</div>
      <ul v-else class="impact__repos">
        <li v-for="repo in affected.slice(0, 6)" :key="repo.name" class="impact__repo">
          <RepoChip :name="repo.name" :format="repo.format" :link="false" />
          <span class="impact__repo-num">{{ formatCount(repo.count) }} · <SizeCell :bytes="repo.bytes" /></span>
          <span class="impact__bar"><span :style="{ width: `${Math.max(2, (repo.bytes / maxBytes) * 100)}%` }" /></span>
        </li>
      </ul>
      <div v-if="totals.truncated" class="impact__warn"><q-icon :name="iconWarning" size="16px" />{{ t('cleanup.impact.truncated') }}</div>
      <q-btn outline no-caps color="primary" class="impact__review" :icon="iconShow" :label="t('cleanup.impact.view')" :disable="totals.count === 0" @click="emit('review')" />
    </template>

    <div class="impact__note"><q-icon :name="iconInfo" size="16px" />{{ note }}</div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { ApiError, errorMessage } from '@/api/client'
import type { CleanupPreview } from '@/api/types'
import RepoChip from '@/components/RepoChip.vue'
import SizeCell from '@/components/SizeCell.vue'
import { t } from '@/i18n'
import { formatCount } from '@/utils/format'
import { iconInfo, iconShow, iconWarning } from '@/utils/icons'

const props = withDefaults(defineProps<{ totals: CleanupPreview | null; loading: boolean; ready: boolean; error: unknown; scope: string; graceHours?: number | null }>(), {
  graceHours: null,
})
const emit = defineEmits<{ review: [] }>()

const affected = computed(() => [...(props.totals?.repositories ?? [])].filter((r) => r.count > 0).sort((a, b) => b.bytes - a.bytes))
const maxBytes = computed(() => Math.max(1, ...affected.value.map((r) => r.bytes)))
const hasFieldErrors = computed(() => props.error instanceof ApiError && Boolean(props.error.problem.errors?.length))
const note = computed(() => (props.graceHours ? t('cleanup.impact.gcNote', { hours: props.graceHours }) : t('cleanup.impact.blobNote')))
</script>

<style scoped>
.impact {
  display: grid;
  gap: 10px;
  align-content: start;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-subtle);
  transition: opacity var(--dur) var(--ease-out);
}

.impact--busy .impact__numbers {
  opacity: 0.6;
}

.impact__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.impact__title {
  font-size: var(--fs-12);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-secondary);
}

.impact__scope {
  margin-top: -6px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.impact__numbers {
  transition: opacity var(--dur) var(--ease-out);
}

.impact__count {
  font-size: var(--fs-24);
  font-weight: 650;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}

.impact__bytes {
  font-size: var(--fs-16);
  font-weight: 600;
}

.impact__across {
  font-weight: 400;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.impact__repos {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 8px;
}

.impact__repo {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 4px 8px;
  align-items: center;
}

.impact__repo-num {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.impact__bar {
  grid-column: 1 / -1;
  height: 4px;
  border-radius: 2px;
  background: var(--border);
  overflow: hidden;
}

.impact__bar span {
  display: block;
  height: 100%;
  border-radius: 2px;
  background: var(--q-primary);
  transition: width 200ms ease-out;
}

.impact__hint {
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.impact__hint--error {
  color: var(--status-blocked);
}

.impact__warn,
.impact__note {
  display: flex;
  gap: 6px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.impact__warn {
  color: var(--status-degraded);
}

.impact__review {
  justify-self: start;
}

.impact__note {
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
</style>
