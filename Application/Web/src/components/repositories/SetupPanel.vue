<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- "Set me up" side panel: a tab per client, copyable snippets, the user's token as a placeholder. -->
<template>
  <q-dialog v-model="open" position="right" full-height :maximized="$q.screen.lt.sm" transition-show="slide-left" transition-hide="slide-right">
    <q-card class="setup" :aria-label="t('setup.title', { name: repo.name })">
      <header class="setup__head">
        <FormatIcon :mark="repoMark(repo)" :size="24" brand />
        <div class="col">
          <div class="setup__title">{{ t('setup.title', { name: repo.name }) }}</div>
          <div class="text-caption">{{ t('setup.caption', { format: formatLabel(repo.format) }) }}</div>
        </div>
        <q-btn v-close-popup flat round dense :icon="iconClose" :aria-label="t('common.close')" />
      </header>

      <div v-if="guide.data.value" class="setup__body">
        <q-tabs v-model="client" dense no-caps align="left" active-color="primary" indicator-color="primary" class="setup__tabs" narrow-indicator>
          <q-tab v-for="c in guide.data.value.clients" :key="c.id" :name="c.id" :label="c.label" />
        </q-tabs>

        <div class="setup__token">
          <q-icon :name="iconToken" size="18px" />
          <div class="col">
            {{ t('setup.tokenNote') }}
            <router-link :to="{ name: 'admin-tokens' }" class="text-link">{{ t('setup.createToken') }}</router-link>
          </div>
        </div>

        <p v-for="note in guide.data.value.notes ?? []" :key="note" class="setup__note">{{ note }}</p>

        <section v-for="s in current?.snippets ?? []" :key="s.id" class="snippet">
          <div class="snippet__head">
            <div class="col">
              <div class="snippet__title">{{ s.title }}</div>
              <div v-if="s.description" class="snippet__desc">{{ s.description }}</div>
            </div>
            <q-btn class="snippet__copy" outline dense no-caps size="sm" :icon="iconCopy" :label="t('common.copy')" @click="copy(fill(s.content), s.title)" />
          </div>
          <div class="snippet__code">
            <div v-if="s.filename" class="snippet__file mono">
              <q-icon :name="iconFile" size="14px" />{{ s.filename }}
            </div>
            <pre class="code-block" :class="{ 'code-block--file': s.filename }"><code>{{ fill(s.content) }}</code></pre>
          </div>
        </section>
      </div>
      <div v-else-if="guide.error.value" class="setup__body text-negative">{{ errorMessage(guide.error.value) }}</div>
      <div v-else class="setup__body">
        <q-skeleton v-for="i in 3" :key="i" height="110px" class="q-mb-md" />
      </div>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { Repository } from '@/api/types'
import FormatIcon from '@/components/FormatIcon.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { formatLabel, repoMark } from '@/utils/domain'
import { iconClose, iconCopy, iconFile, iconToken } from '@/utils/icons'

import { fillSnippet } from './snippets'

const props = defineProps<{ repo: Repository }>()
const open = defineModel<boolean>({ required: true })

const auth = useAuthStore()
const copy = useCopy()
const client = ref<string>('')
const guide = useAsync(() => call(api.GET('/repositories/{name}/setup', { params: { path: { name: props.repo.name } } })), [() => props.repo.name])

watch(
  () => guide.data.value,
  (g) => {
    if (g && !g.clients.some((c) => c.id === client.value)) client.value = g.clients[0]?.id ?? ''
  },
  { immediate: true },
)

const current = computed(() => guide.data.value?.clients.find((c) => c.id === client.value))
const fill = (content: string) => fillSnippet(content, auth.user?.username ?? null)
</script>

<style scoped>
.setup {
  width: 620px;
  max-width: 100vw;
  height: 100%;
  display: flex;
  flex-direction: column;
  border-radius: 0 !important;
}

.setup__head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
}

.setup__title {
  font-size: var(--fs-16);
  font-weight: 650;
}

.setup__body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 20px 24px;
}

.setup__tabs {
  margin-bottom: 14px;
  border-bottom: 1px solid var(--border);
}

.setup__token {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  margin-bottom: 16px;
  border-radius: var(--radius-sm);
  background: var(--surface-brand);
  color: var(--text-primary);
  font-size: var(--fs-13);
}

.setup__token .q-icon {
  color: var(--q-primary);
  margin-top: 1px;
}

.setup__note {
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.snippet {
  margin-bottom: 20px;
}

.snippet__head {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  margin-bottom: 8px;
}

.snippet__title {
  font-weight: 600;
}

.snippet__desc {
  margin-top: 2px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.snippet__code {
  position: relative;
}

.snippet__file {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-bottom: 0;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  background: var(--surface-subtle);
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.code-block--file {
  border-top-left-radius: 0;
  border-top-right-radius: 0;
}

.snippet__copy {
  flex: none;
  color: var(--text-secondary);
  padding: 0 8px;
}
</style>
