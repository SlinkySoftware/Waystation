<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Search (UI_DESIGN §4.5): one box, facet sidebar, keyboard-navigable results that open the Browse view. All state
     lives in the URL (shareable, back button works). "/" focuses the box, ↓ moves into the results. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.search')" :caption="t('search.caption')" />
    <div class="search" :class="{ 'search--stacked': stacked }">
      <aside v-if="!stacked" class="search__facets">
        <SearchFacetPanel :facets="facets" :state="state" @toggle="toggleFacet" @cached="setCached" @clear="clearFilters" />
      </aside>
      <section class="search__main">
        <div @keydown="onBoxKey">
          <q-input
            ref="box"
            :model-value="draft"
            outlined
            clearable
            autofocus
            :placeholder="t('search.placeholder')"
            :aria-label="t('search.label')"
            class="search__box"
            :input-attrs="{ spellcheck: 'false', autocomplete: 'off', 'aria-keyshortcuts': '/' }"
            @update:model-value="onInput"
          >
            <template #prepend><q-icon :name="iconSearch" /></template>
          </q-input>
        </div>
        <q-expansion-item v-if="stacked" dense :label="t('search.filters')" :caption="filterCaption" class="search__mobile-facets">
          <SearchFacetPanel :facets="facets" :state="state" @toggle="toggleFacet" @cached="setCached" @clear="clearFilters" />
        </q-expansion-item>
        <div class="search__status" aria-live="polite">
          <template v-if="active && facets">{{ countLabel }}</template>
          <span v-if="mode === 'digest'" class="ws-badge tone-hit search__digest">{{ t('search.digestMode') }}</span>
          <span class="search__keys gt-sm">{{ t('search.keyboardHelp') }}</span>
        </div>

        <PageCard flush>
          <EmptyState v-if="!active" illustration="search" :title="t('search.startTitle')" :text="t('search.startText')">
            <span class="search__try">{{ t('search.examples') }}</span>
            <q-btn v-for="ex in examples" :key="ex" outline no-caps dense color="primary" :label="ex" class="mono" @click="apply(ex, true)" />
          </EmptyState>
          <EmptyState v-else-if="error" illustration="error" :title="t('search.failed')" :text="errorMessage(error)">
            <q-btn outline no-caps color="primary" :label="t('common.retry')" @click="load(false)" />
          </EmptyState>
          <template v-else>
            <div v-if="loading && !items.length" class="search__skeletons">
              <q-skeleton v-for="i in 6" :key="i" height="52px" />
            </div>
            <EmptyState
              v-else-if="!items.length"
              illustration="search"
              :title="state.q.trim() ? t('search.noResults', { q: state.q.trim() }) : t('search.noResultsFiltered')"
              :text="t('search.noResultsText')"
            >
              <q-btn v-if="filtered" outline no-caps color="primary" :label="t('search.clearFilters')" @click="clearFilters" />
            </EmptyState>
            <ul v-else ref="list" class="search__results" :aria-label="t('search.results')" :aria-busy="loading" @keydown="onListKey">
              <li v-for="hit in items" :key="hitKey(hit)">
                <SearchResultRow :hit="hit" :mark="repos.markFor(hit.repository, hit.format)" :query="state.q" @open="remember" />
              </li>
            </ul>
            <div v-if="next" class="search__more">
              <q-btn outline no-caps color="primary" :loading="loading" :label="t('common.loadMore')" @click="load(true)" />
            </div>
          </template>
        </PageCard>
      </section>
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { type QInput, useQuasar } from 'quasar'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, call, errorMessage } from '@/api/client'
import type { SearchFacets, SearchHit, SearchPage as Page } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import { resultItem } from '@/components/search/palette'
import SearchFacetPanel from '@/components/search/SearchFacetPanel.vue'
import SearchResultRow from '@/components/search/SearchResultRow.vue'
import { t } from '@/i18n'
import { useRecentStore } from '@/stores/recent'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatCount } from '@/utils/format'
import { iconSearch } from '@/utils/icons'
import { type FacetName, hasFilters, isActive, type SearchState, stateFromQuery, stateToQuery, toggle } from '@/utils/search'

const PAGE = 50
const examples = ['nginx', 'react', 'openssl']

const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const repos = useRepositoriesStore()
const recent = useRecentStore()
void repos.load().catch(() => undefined)

const state = computed(() => stateFromQuery(route.query))
const active = computed(() => isActive(state.value))
const filtered = computed(() => hasFilters(state.value))
const stacked = computed(() => $q.screen.lt.md)
const draft = ref(state.value.q)
const box = ref<QInput | null>(null)
const list = ref<HTMLElement | null>(null)

const items = shallowRef<SearchHit[]>([])
const facets = shallowRef<SearchFacets | null>(null)
const mode = ref<Page['mode']>('name')
const next = ref<string | null>(null)
const loading = ref(false)
const error = ref<unknown>(null)
let generation = 0
let timer: ReturnType<typeof setTimeout> | undefined

const countLabel = computed(() => {
  const total = facets.value?.total ?? 0
  return facets.value?.capped ? t('search.countCapped', { count: formatCount(total) }) : t('search.count', { count: total })
})
const filterCaption = computed(() => {
  const n = state.value.format.length + state.value.repository.length + state.value.kind.length + state.value.type.length + (state.value.cached ? 1 : 0)
  return n ? String(n) : ''
})

function navigate(next: SearchState, replace = false): void {
  const location = { name: 'search', query: stateToQuery(next) }
  void (replace ? router.replace(location) : router.push(location))
}

function apply(q: string | null, push = false): void {
  clearTimeout(timer)
  draft.value = q ?? ''
  if ((q ?? '') !== state.value.q) navigate({ ...state.value, q: q ?? '' }, !push)
}

function onInput(value: string | number | null): void {
  draft.value = value == null ? '' : String(value)
  clearTimeout(timer)
  timer = setTimeout(() => apply(draft.value), 250)
}

function toggleFacet(facet: FacetName, value: string): void {
  navigate({ ...state.value, [facet]: toggle(state.value[facet] as string[], value) })
}

function setCached(value: boolean): void {
  navigate({ ...state.value, cached: value })
}

function clearFilters(): void {
  navigate({ q: state.value.q, format: [], repository: [], kind: [], type: [], cached: false })
}

async function load(more: boolean): Promise<void> {
  const mine = ++generation
  const s = state.value
  loading.value = true
  if (!more) error.value = null
  try {
    const page = await call(
      api.GET('/search', {
        params: {
          query: {
            q: s.q,
            format: s.format.length ? s.format : undefined,
            repository: s.repository.length ? s.repository : undefined,
            kind: s.kind.length ? s.kind : undefined,
            type: s.type.length ? s.type : undefined,
            cached: s.cached || undefined,
            cursor: more ? (next.value ?? undefined) : undefined,
            limit: active.value ? PAGE : 1,
          },
        },
      }),
    )
    if (mine !== generation) return
    items.value = more ? [...items.value, ...page.items] : page.items
    if (!more) facets.value = page.facets ?? null
    mode.value = page.mode
    next.value = page.next_cursor
  } catch (cause) {
    if (mine === generation) error.value = cause
  } finally {
    if (mine === generation) loading.value = false
  }
}

watch(
  () => JSON.stringify(stateToQuery(state.value)),
  () => {
    if (state.value.q !== draft.value && document.activeElement?.tagName !== 'INPUT') draft.value = state.value.q
    next.value = null
    void load(false)
  },
  { immediate: true },
)

function hitKey(hit: SearchHit): string {
  return `${hit.repository}|${hit.kind}|${hit.name}|${hit.version ?? ''}|${hit.qualifier ?? ''}|${hit.path ?? ''}`
}

function remember(hit: SearchHit): void {
  const item = resultItem(hit, repos.markFor(hit.repository, hit.format))
  if (item.remember) recent.add(item.remember)
}

function rows(): HTMLAnchorElement[] {
  return Array.from(list.value?.querySelectorAll<HTMLAnchorElement>('a.result') ?? [])
}

function focusResult(index: number): void {
  const all = rows()
  all[Math.max(0, Math.min(index, all.length - 1))]?.focus()
}

function onBoxKey(event: KeyboardEvent): void {
  if (event.key === 'Enter') {
    event.preventDefault()
    apply(draft.value, true)
  } else if (event.key === 'ArrowDown' && rows().length) {
    event.preventDefault()
    focusResult(0)
  }
}

function onListKey(event: KeyboardEvent): void {
  const all = rows()
  const at = all.indexOf(document.activeElement as HTMLAnchorElement)
  const moves: Record<string, number> = { ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: all.length - 1 }
  if (event.key in moves) {
    event.preventDefault()
    if (moves[event.key]! < 0) box.value?.focus()
    else focusResult(moves[event.key]!)
  } else if (event.key === 'Escape') {
    event.preventDefault()
    box.value?.focus()
  }
}

function onSlash(event: KeyboardEvent): void {
  const target = event.target as HTMLElement | null
  if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) return
  if (target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return
  event.preventDefault()
  box.value?.focus()
  void nextTick(() => box.value?.select())
}

onMounted(() => window.addEventListener('keydown', onSlash))
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onSlash)
  clearTimeout(timer)
})
</script>

<style scoped>
.search {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}

.search--stacked {
  grid-template-columns: minmax(0, 1fr);
}

.search__facets {
  position: sticky;
  top: 72px;
}

.search__main {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

.search__box :deep(.q-field__control) {
  background: var(--surface);
}

.search__status {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 22px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.search__digest {
  font-size: 11px;
  height: 20px;
  padding: 0 8px;
}

.search__keys {
  margin-left: auto;
  font-size: var(--fs-12);
  color: var(--text-tertiary);
}

.search__results {
  list-style: none;
  margin: 0;
  padding: 0;
}

.search__results li:first-child :deep(.result) {
  border-top: 0;
}

.search__skeletons {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px;
}

.search__more {
  display: flex;
  justify-content: center;
  padding: 12px;
  border-top: 1px solid var(--border);
}

.search__try {
  align-self: center;
  color: var(--text-secondary);
}

.search__mobile-facets {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.search__mobile-facets :deep(.q-expansion-item__content) {
  padding: 8px 16px 16px;
}
</style>
