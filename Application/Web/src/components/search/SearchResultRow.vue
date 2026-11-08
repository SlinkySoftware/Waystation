<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- One search hit: a link into the repository's Browse view. Icon, name (matched part emphasised; text only, never
     HTML), version/tag, where it is, cache state, size and last update. -->
<template>
  <router-link :to="browseLocation(hit)" class="result" @click="emit('open', hit)">
    <FormatIcon :mark="mark" :size="24" brand decorative class="result__icon" />
    <div class="result__main">
      <div class="result__title">
        <span class="result__name" :class="{ mono: hit.kind === 'file' }">
          <template v-for="(part, i) in parts" :key="i"><mark v-if="part.match">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template>
        </span>
        <span v-if="hit.version" class="result__version mono">{{ hit.version }}</span>
        <span class="result__kind">{{ t(`search.kinds.${hit.kind}`) }}</span>
      </div>
      <div class="result__meta">
        <RepoChip :name="hit.repository" :format="hit.format" :mark-override="mark" :link="false" />
        <span v-if="hit.qualifier" class="mono">{{ hit.qualifier }}</span>
        <span v-if="hit.path" class="result__path mono" :title="hit.path">{{ hit.path }}</span>
        <span v-else-if="hit.sha256 && hit.kind === 'image'" class="result__path mono" :title="hit.sha256">{{ hit.sha256.slice(0, 19) }}</span>
      </div>
    </div>
    <div class="result__side">
      <span v-if="hit.cached === false" class="ws-badge tone-offline result__badge">{{ t('search.listed') }}<q-tooltip>{{ t('search.listedHelp') }}</q-tooltip></span>
      <span v-else-if="hit.cached" class="ws-badge tone-hit result__badge">{{ t('search.cached') }}</span>
      <SizeCell :bytes="hit.size" class="result__size" />
      <TimeCell :value="hit.updated_at" class="result__time gt-xs" />
    </div>
  </router-link>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { SearchHit } from '@/api/types'
import type { BrandMark } from '@/components/brandMarks'
import FormatIcon from '@/components/FormatIcon.vue'
import RepoChip from '@/components/RepoChip.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { t } from '@/i18n'
import { browseLocation, highlight } from '@/utils/search'

const props = defineProps<{ hit: SearchHit; mark: BrandMark; query: string }>()
const emit = defineEmits<{ open: [hit: SearchHit] }>()

const parts = computed(() => highlight(props.hit.name, props.query))
</script>

<style scoped>
.result {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 16px;
  border-top: 1px solid var(--border);
  color: var(--text-primary);
  text-decoration: none;
  outline: none;
}

.result:hover {
  background: var(--hover-bg);
}

.result:focus-visible {
  background: var(--selected-bg);
  box-shadow: inset 3px 0 0 var(--q-primary);
}

.result__icon {
  flex: none;
}

.result__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.result__title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.result__name {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.result__name mark {
  background: transparent;
  color: var(--q-primary);
  text-decoration: underline;
  text-decoration-thickness: 2px;
  text-underline-offset: 3px;
}

.result__version {
  flex: none;
  padding: 0 6px;
  border-radius: 4px;
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  font-size: var(--fs-12);
}

.result__kind {
  flex: none;
  font-size: var(--fs-12);
  color: var(--text-tertiary);
}

.result__meta {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.result__path {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.result__side {
  flex: none;
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: var(--fs-13);
}

.result__badge {
  font-size: 11px;
  height: 20px;
  padding: 0 7px;
}

.result__size {
  min-width: 72px;
  text-align: right;
}

.result__time {
  min-width: 84px;
  color: var(--text-secondary);
}

@media (max-width: 599px) {
  .result__side .result__size,
  .result__meta .result__path {
    display: none;
  }
}
</style>
