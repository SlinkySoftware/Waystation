<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Your browser sessions (UI_DESIGN §4.11): where you are signed in, end one, or sign out everywhere else. -->
<template>
  <PageCard :title="t('ui.profile.sessions')" :caption="t('ui.profile.sessionsCaption')">
    <template #actions>
      <q-btn v-if="others" outline no-caps dense class="q-px-sm" :icon="iconLogout" :label="t('ui.profile.signOutOthers')" :loading="busy === 'all'" @click="endOthers" />
    </template>
    <LoadFailedState v-if="state.error.value && !state.data.value" compact :error="state.error.value" :title="t('ui.profile.sessionsFailed')" @retry="state.reload" />
    <q-skeleton v-else-if="!state.data.value" height="64px" />
    <div v-else class="my-sessions">
      <SessionsTable :sessions="state.data.value" />
      <ul class="my-sessions__end">
        <li v-for="s in state.data.value.filter((x) => !x.current)" :key="s.id">
          <q-btn flat dense no-caps size="sm" :label="t('ui.profile.endSession', { browser: browserLabel(s.user_agent) ?? t('admin.sessions.unknownBrowser') })" :loading="busy === s.id" @click="end(s.id)" />
        </li>
      </ul>
    </div>
  </PageCard>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { api, call } from '@/api/client'
import SessionsTable from '@/components/admin/SessionsTable.vue'
import LoadFailedState from '@/components/LoadFailedState.vue'
import PageCard from '@/components/PageCard.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { iconLogout } from '@/utils/icons'
import { browserLabel } from '@/utils/userAgent'

const notify = useNotify()
const state = useAsync(() => call(api.GET('/auth/sessions')))
const others = computed(() => (state.data.value ?? []).some((s) => !s.current))
const busy = ref<string | null>(null)

async function end(id: string): Promise<void> {
  busy.value = id
  try {
    await call(api.DELETE('/auth/sessions/{session_id}', { params: { path: { session_id: id } } }))
    notify.success(t('ui.profile.sessionEnded'))
    await state.reload()
  } catch (error) {
    notify.error(error)
  } finally {
    busy.value = null
  }
}

async function endOthers(): Promise<void> {
  busy.value = 'all'
  try {
    const result = await call(api.DELETE('/auth/sessions'))
    notify.success(t('ui.profile.othersEnded', { count: result.revoked }))
    await state.reload()
  } catch (error) {
    notify.error(error)
  } finally {
    busy.value = null
  }
}
</script>

<style scoped>
.my-sessions__end {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}
</style>
