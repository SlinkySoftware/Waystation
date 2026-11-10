<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Command palette (Ctrl/⌘ K, UI_DESIGN §3): jump to pages, repositories and recent items, run actions, and quick-search
     packages, images and files. ARIA combobox + listbox; focus stays in the input, arrows move the active option. -->
<template>
  <Teleport to="body">
    <div v-if="open" class="palette-backdrop" @mousedown.self="close">
      <div class="palette" role="dialog" aria-modal="true" :aria-label="t('palette.title')" @keydown.tab.prevent>
        <div class="palette__search">
          <q-icon :name="iconSearch" size="20px" class="text-faint" />
          <input
            ref="input"
            v-model="query"
            class="palette__input"
            type="text"
            role="combobox"
            aria-autocomplete="list"
            aria-expanded="true"
            :aria-controls="listId"
            :aria-activedescendant="activeId ? optionId(activeId) : undefined"
            :placeholder="t('palette.placeholder')"
            autocomplete="off"
            spellcheck="false"
            @keydown="onKey"
          />
          <q-spinner v-if="results.loading" size="16px" color="primary" />
          <kbd>esc</kbd>
        </div>
        <div :id="listId" ref="list" class="palette__list" role="listbox" :aria-label="t('palette.results')">
          <div v-for="section in sections" :key="section.group" role="group" :aria-labelledby="`${listId}-${section.group}`">
            <div :id="`${listId}-${section.group}`" class="palette__group">{{ section.label }}</div>
            <div
              v-for="item in section.items"
              :id="optionId(item.id)"
              :key="item.id"
              role="option"
              :aria-selected="item.id === activeId"
              class="palette__item"
              :class="{ 'palette__item--active': item.id === activeId }"
              @mousemove="hover(item.id)"
              @click="choose(item)"
            >
              <FormatIcon v-if="item.mark" :mark="item.mark" :size="18" brand decorative />
              <q-icon v-else :name="item.icon" size="18px" class="text-faint" />
              <span class="palette__label">{{ item.label }}</span>
              <span v-if="item.detail" class="palette__detail">{{ item.detail }}</span>
              <span v-if="item.hint" class="palette__hint">{{ item.hint }}</span>
            </div>
          </div>
          <div v-if="!flat.length" class="palette__empty">{{ results.loading ? t('common.loading') : t('palette.noMatches') }}</div>
        </div>
        <div class="palette__foot" aria-hidden="true">
          <span><kbd>↑</kbd><kbd>↓</kbd> {{ t('palette.navigate') }}</span>
          <span><kbd>↵</kbd> {{ t('palette.open') }}</span>
          <span><kbd>esc</kbd> {{ t('palette.close') }}</span>
        </div>
        <div class="visually-hidden" aria-live="polite">{{ announcement }}</div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import FormatIcon from '@/components/FormatIcon.vue'
import { t } from '@/i18n'
import { navigation } from '@/layouts/navigation'
import { useAuthStore } from '@/stores/auth'
import { useRecentStore } from '@/stores/recent'
import { useRepositoriesStore } from '@/stores/repositories'
import { useThemeStore } from '@/stores/theme'
import { iconSearch } from '@/utils/icons'

import { actionItems, moveActive, type PaletteItem, pageItems, rank, recentItem, repositoryItem, searchItem, toSections } from './palette'
import { useQuickSearch } from './useQuickSearch'

const open = defineModel<boolean>({ required: true })

const router = useRouter()
const auth = useAuthStore()
const recent = useRecentStore()
const repos = useRepositoriesStore()
const theme = useThemeStore()
const listId = 'ws-palette-list'
const query = ref('')
const activeId = ref<string | null>(null)
const input = ref<HTMLInputElement | null>(null)
const list = ref<HTMLElement | null>(null)
let restoreFocus: HTMLElement | null = null

const results = useQuickSearch(query, (name, format) => repos.markFor(name, format))
const can = (capability: string) => auth.can(capability)

const items = computed<PaletteItem[]>(() => {
  const q = query.value.trim()
  const pages = pageItems(navigation, can)
  const actions = actionItems(can)
  const repositories = repos.items.map(repositoryItem)
  if (!q) return [...recent.items.slice(0, 5).map(recentItem), ...repositories.slice(0, 5), ...pages, ...actions]
  // Package search needs some visible repository (`repositories.browse`); without it only pages and actions match.
  const search = can('repositories.browse') ? [searchItem(q)] : []
  return [...search, ...rank(repositories, q, 6), ...rank(pages, q, 4), ...results.items, ...rank(actions, q, 3)]
})
const sections = computed(() => toSections(items.value))
const flat = computed(() => sections.value.flatMap((s) => s.items))
const announcement = computed(() => (open.value ? t('palette.count', { count: flat.value.length }) : ''))

function optionId(id: string): string {
  return `ws-palette-${id.replace(/[^A-Za-z0-9_-]/g, '_')}`
}

// The first option stays active while suggestions arrive, until the user picks another one (keys or pointer).
let moved = false
watch(query, () => {
  moved = false
})
watch(flat, (now) => {
  if (!moved || !now.some((i) => i.id === activeId.value)) activeId.value = now[0]?.id ?? null
})

function hover(id: string): void {
  if (activeId.value === id) return
  moved = true
  activeId.value = id
}

watch(activeId, async (id) => {
  await nextTick()
  if (id) document.getElementById(optionId(id))?.scrollIntoView?.({ block: 'nearest' })
})

watch(
  open,
  async (now) => {
    if (now) {
      restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
      query.value = ''
      moved = false
      activeId.value = flat.value[0]?.id ?? null
      void repos.load().catch(() => undefined)
      await nextTick()
      input.value?.focus()
    } else {
      restoreFocus?.focus?.()
      restoreFocus = null
    }
  },
  { immediate: true },
)

function close(): void {
  open.value = false
}

function choose(item: PaletteItem): void {
  if (item.remember) recent.add(item.remember)
  close()
  if (item.command === 'theme') theme.cycle()
  else if (item.to) void router.push(item.to)
}

function onKey(event: KeyboardEvent): void {
  const moves: Record<string, number | 'start' | 'end'> = { ArrowDown: 1, ArrowUp: -1, PageDown: 5, PageUp: -5 }
  if (event.key in moves) {
    event.preventDefault()
    moved = true
    activeId.value = moveActive(flat.value, activeId.value, moves[event.key]!)
  } else if ((event.key === 'Home' || event.key === 'End') && event.ctrlKey) {
    event.preventDefault()
    moved = true
    activeId.value = moveActive(flat.value, activeId.value, event.key === 'Home' ? 'start' : 'end')
  } else if (event.key === 'Enter') {
    event.preventDefault()
    const item = flat.value.find((i) => i.id === activeId.value)
    if (item) choose(item)
  } else if (event.key === 'Escape') {
    event.preventDefault()
    close()
  }
}
</script>

<style scoped>
.palette-backdrop {
  position: fixed;
  inset: 0;
  z-index: 6000;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 12vh 16px 16px;
  background: rgba(15, 23, 42, 0.32);
  backdrop-filter: blur(2px);
  animation: fade var(--dur) var(--ease-out);
}

.palette {
  display: flex;
  flex-direction: column;
  width: min(640px, 100%);
  max-height: 70vh;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface-raised);
  box-shadow: var(--shadow-pop);
  overflow: hidden;
}

.palette__search {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
}

.palette__input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--text-primary);
  font: inherit;
  font-size: var(--fs-16);
}

.palette__list {
  flex: 1;
  overflow-y: auto;
  padding: 6px;
}

.palette__group {
  padding: 10px 10px 4px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

.palette__item {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 40px;
  padding: 6px 10px;
  border-radius: 8px;
  cursor: pointer;
  color: var(--text-primary);
}

.palette__item--active {
  background: var(--selected-bg);
  box-shadow: inset 2px 0 0 var(--q-primary);
}

.palette__label {
  font-weight: 550;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.palette__detail {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.palette__hint {
  margin-left: auto;
  font-size: var(--fs-12);
  color: var(--text-tertiary);
  white-space: nowrap;
}

.palette__empty {
  padding: 28px 12px;
  text-align: center;
  color: var(--text-secondary);
}

.palette__foot {
  display: flex;
  gap: 16px;
  padding: 8px 14px;
  border-top: 1px solid var(--border);
  font-size: var(--fs-12);
  color: var(--text-tertiary);
}

kbd {
  font-family: var(--font-sans);
  font-size: 11px;
  padding: 0 5px;
  margin-right: 2px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--surface-subtle);
  color: var(--text-secondary);
}

@keyframes fade {
  from {
    opacity: 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  .palette-backdrop {
    animation: none;
  }
}
</style>
