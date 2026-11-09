<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<template>
  <nav class="rail" :class="{ 'rail--mini': mini }">
    <template v-for="(group, g) in groups" :key="g">
      <div v-if="group.label" class="rail__group">
        <span v-if="!mini">{{ t(group.label) }}</span>
        <q-separator v-else />
      </div>
      <router-link
        v-for="item in group.items"
        :key="item.name"
        v-slot="{ href, navigate }"
        :to="item.to"
        custom
      >
        <a
          :href="href"
          class="rail__item"
          :class="{ 'rail__item--active': isActive(item), 'rail__item--planned': item.planned }"
          :aria-current="isActive(item) ? 'page' : undefined"
          @click="navigate"
        >
          <q-icon :name="item.icon" size="20px" />
          <span v-if="!mini" class="rail__label">{{ t(item.label) }}</span>
          <span v-if="!mini && item.planned" class="rail__soon">{{ t('nav.soon') }}</span>
          <q-tooltip v-if="mini" anchor="center right" self="center left" :offset="[10, 0]">
            {{ t(item.label) }}{{ item.planned ? ` (${t('nav.soon')})` : '' }}
          </q-tooltip>
        </a>
      </router-link>
    </template>
  </nav>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { t } from '@/i18n'
import { type NavItem, navigation } from '@/layouts/navigation'
import { useAuthStore } from '@/stores/auth'

defineProps<{ mini: boolean }>()

const auth = useAuthStore()
const route = useRoute()

const groups = computed(() =>
  navigation.map((g) => ({ ...g, items: g.items.filter((i) => !i.capability || auth.can(i.capability)) })).filter((g) => g.items.length),
)

function isActive(item: NavItem): boolean {
  if (item.name === 'repositories') return route.name === 'repositories' || (route.name === 'repository' && route.params.tab !== 'browse')
  if (item.name === 'browse') return route.name === 'browse' || (route.name === 'repository' && route.params.tab === 'browse')
  return route.name === item.name || route.meta.nav === item.name
}
</script>

<style scoped>
.rail {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 12px 10px 72px;
}

.rail__group {
  margin: 14px 10px 6px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

.rail--mini .rail__group {
  margin: 10px 4px;
}

.rail__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  height: 38px;
  padding: 0 10px;
  border-radius: 8px;
  color: var(--text-secondary);
  text-decoration: none;
  font-weight: 520;
  font-size: var(--fs-14);
  transition: background var(--dur) var(--ease-out), color var(--dur) var(--ease-out);
}

.rail--mini .rail__item {
  justify-content: center;
  padding: 0;
}

.rail__item:hover {
  background: var(--hover-bg);
  color: var(--text-primary);
}

.rail__item--active {
  background: var(--selected-bg);
  color: var(--q-primary);
  font-weight: 600;
}

.rail__item--active::before {
  content: '';
  position: absolute;
  left: -10px;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--q-primary);
}

.rail__item--planned:not(.rail__item--active) {
  color: var(--text-tertiary);
}

.rail__label {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.rail__soon {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.03em;
  padding: 1px 6px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--text-tertiary);
}
</style>
