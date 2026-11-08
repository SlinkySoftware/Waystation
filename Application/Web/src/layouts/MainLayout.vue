<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- App shell: collapsible left rail (icons at 1024-1439 px, drawer below 1024 px), top bar, live drawer. -->
<template>
  <q-layout view="hHh Lpr lFf" class="shell">
    <q-header class="shell__header">
      <AppTopBar :rail-open="railOpen" @toggle-rail="toggleRail" @open-live="liveOpen = true" />
    </q-header>

    <q-drawer
      v-model="railOpen"
      show-if-above
      :mini="mini"
      :width="232"
      :mini-width="64"
      :breakpoint="1023"
      class="shell__rail"
      :aria-label="t('nav.main')"
    >
      <AppRail :mini="mini" />
      <div v-if="!$q.screen.lt.md" class="shell__rail-foot">
        <q-btn flat round dense :icon="mini ? iconMenu : iconMenuOpen" :aria-label="mini ? t('nav.expand') : t('nav.collapse')" @click="toggleMini">
          <q-tooltip anchor="center right" self="center left">{{ mini ? t('nav.expand') : t('nav.collapse') }}</q-tooltip>
        </q-btn>
      </div>
    </q-drawer>

    <q-drawer v-model="liveOpen" side="right" overlay elevated :width="$q.screen.lt.sm ? $q.screen.width : 520" behavior="mobile" class="shell__live">
      <LiveDrawer v-if="liveOpen" @close="liveOpen = false" />
    </q-drawer>

    <q-page-container>
      <router-view v-slot="{ Component, route }">
        <transition name="page" mode="out-in">
          <component :is="Component" :key="route.name === 'repository' ? `repo-${route.params.name}` : String(route.name)" />
        </transition>
      </router-view>
    </q-page-container>
  </q-layout>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, ref, watch } from 'vue'

import AppRail from '@/components/shell/AppRail.vue'
import AppTopBar from '@/components/shell/AppTopBar.vue'
import LiveDrawer from '@/components/shell/LiveDrawer.vue'
import { useLiveTopic } from '@/composables/useLiveTopic'
import { t } from '@/i18n'
import { iconMenu, iconMenuOpen } from '@/utils/icons'
import { readString, writeString } from '@/utils/storage'

defineOptions({ name: 'MainLayout' })

const $q = useQuasar()
const RAIL_KEY = 'ws.rail'

const railOpen = ref(false)
const liveOpen = ref(false)
// Collapsed by preference, or forced to icons between 1024 and 1439 px (UI_DESIGN §5).
const collapsed = ref(readString(RAIL_KEY) === 'collapsed')
const mini = computed(() => !$q.screen.lt.md && (collapsed.value || $q.screen.width < 1440))

// The shell keeps the stream open for the live indicator; pages add their own topics.
useLiveTopic(['transfers', 'stats', 'activity'])

function toggleRail(): void {
  if ($q.screen.lt.md) railOpen.value = !railOpen.value
  else toggleMini()
}

function toggleMini(): void {
  collapsed.value = !mini.value
  writeString(RAIL_KEY, collapsed.value ? 'collapsed' : null)
}

watch(
  () => $q.screen.width >= 1440,
  (wide) => {
    if (wide && readString(RAIL_KEY) !== 'collapsed') collapsed.value = false
  },
)
</script>

<style scoped>
.shell__header {
  background: var(--header-bg);
  backdrop-filter: saturate(180%) blur(12px);
  color: var(--text-primary);
  border-bottom: 1px solid var(--border);
}

.shell :deep(.shell__rail) {
  background: var(--rail-bg);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
}

.shell__rail-foot {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  background: var(--rail-bg);
}

.shell :deep(.shell__live) {
  background: var(--page-bg);
}

.page-enter-active,
.page-leave-active {
  transition: opacity 120ms var(--ease-out);
}

.page-enter-from,
.page-leave-to {
  opacity: 0;
}
</style>
