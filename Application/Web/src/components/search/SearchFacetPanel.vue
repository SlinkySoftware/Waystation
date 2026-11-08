<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Search facet sidebar: format, repository, kind and repository type with counts (each counted with the other
     filters applied), plus "cached only". Selecting values within a facet ORs them; facets AND together. -->
<template>
  <nav class="facets" :aria-label="t('search.filters')">
    <div class="facets__head">
      <span class="facets__title">{{ t('search.filters') }}</span>
      <q-btn v-if="filtered" flat dense no-caps size="sm" color="primary" :label="t('search.clearFilters')" @click="emit('clear')" />
    </div>
    <q-toggle :model-value="state.cached" dense :label="t('search.cachedOnly')" class="facets__cached" @update:model-value="emit('cached', $event)">
      <q-tooltip max-width="280px">{{ t('search.cachedOnlyHelp') }}</q-tooltip>
    </q-toggle>
    <fieldset v-for="facet in groups" :key="facet.name" class="facet">
      <legend class="facet__legend">{{ t(`search.facets.${facet.name}`) }}</legend>
      <q-skeleton v-if="!facets" type="text" width="70%" />
      <label v-for="v in facet.values" :key="v.value" class="facet__value" :class="{ 'facet__value--zero': v.count === 0 && !v.selected }">
        <input type="checkbox" class="facet__check" :checked="v.selected" @change="emit('toggle', facet.name, v.value)" />
        <FormatIcon v-if="v.mark" :mark="v.mark" :size="16" decorative />
        <span class="facet__label" :title="v.label">{{ v.label }}</span>
        <span class="facet__count">{{ formatCount(v.count) }}{{ capped ? '+' : '' }}</span>
      </label>
      <q-btn v-if="facet.more" flat dense no-caps size="sm" :label="t('search.showAll')" class="facet__more" @click="expanded = [...expanded, facet.name]" />
    </fieldset>
  </nav>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import type { Format, SearchFacets } from '@/api/types'
import FormatIcon from '@/components/FormatIcon.vue'
import type { BrandMark } from '@/components/brandMarks'
import { t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatLabel, formatMark } from '@/utils/domain'
import { formatCount } from '@/utils/format'
import { FACETS, type FacetName, hasFilters, type SearchState } from '@/utils/search'

const props = defineProps<{ facets: SearchFacets | null; state: SearchState }>()
const emit = defineEmits<{ toggle: [facet: FacetName, value: string]; cached: [value: boolean]; clear: [] }>()

const repos = useRepositoriesStore()
const expanded = ref<FacetName[]>([])
const SHOWN = 8

const filtered = computed(() => hasFilters(props.state))
const capped = computed(() => props.facets?.capped ?? false)

function label(facet: FacetName, value: string): string {
  if (facet === 'format') return formatLabel(value as Format)
  if (facet === 'kind') return t(`search.kinds.${value as 'package' | 'image' | 'file'}`)
  if (facet === 'type') return t(`repoType.${value as 'proxy' | 'hosted' | 'group'}`)
  return value
}

function mark(facet: FacetName, value: string): BrandMark | undefined {
  if (facet === 'format') return formatMark(value as Format)
  if (facet === 'repository') {
    const repo = repos.byName.get(value)
    return repo ? repos.markFor(value, repo.format) : undefined
  }
  return undefined
}

const groups = computed(() =>
  FACETS.map((name) => {
    const selected = props.state[name] as readonly string[]
    const values = (props.facets?.[name] ?? []).map((v) => ({ value: v.value, count: v.count, selected: selected.includes(v.value), label: label(name, v.value), mark: mark(name, v.value) }))
    const all = expanded.value.includes(name) ? values : values.filter((v, i) => i < SHOWN || v.selected)
    return { name, values: all, more: all.length < values.length }
  }).filter((g) => !props.facets || g.values.length > 0),
)
</script>

<style scoped>
.facets {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.facets__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 28px;
}

.facets__title {
  font-weight: 650;
}

.facet {
  margin: 0;
  padding: 0;
  border: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.facet__legend {
  margin-bottom: 4px;
  padding: 0;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

.facet__value {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
  padding: 0 6px;
  border-radius: 6px;
  cursor: pointer;
}

.facet__value:hover {
  background: var(--hover-bg);
}

.facet__value--zero {
  color: var(--text-tertiary);
}

.facet__check {
  width: 16px;
  height: 16px;
  margin: 0;
  accent-color: var(--q-primary);
}

.facet__label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.facet__count {
  font-size: var(--fs-12);
  font-variant-numeric: tabular-nums;
  color: var(--text-secondary);
}

.facet__more {
  align-self: flex-start;
}
</style>
