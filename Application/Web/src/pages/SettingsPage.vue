<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Administration → Settings (UI_DESIGN §4.10, SPEC §4.12): the instance name, the two-factor policy, docker routing,
     and (read-only, from
     the environment) sign-in, session, retention and storage limits. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.settings')" :caption="t('admin.settings.caption')" />

    <q-card v-if="state.error.value && !state.data.value">
      <EmptyState illustration="error" :title="t('admin.settings.loadFailed')" :text="errorMessage(state.error.value)">
        <q-btn flat no-caps :label="t('common.retry')" @click="state.reload" />
      </EmptyState>
    </q-card>

    <div v-else class="settings">
      <PageCard :title="t('admin.settings.general')" :caption="t('admin.settings.generalCaption')">
        <ProblemBanner :error="problem" />
        <q-form class="settings__general" @submit="save">
          <q-input
            v-model="instanceName"
            outlined
            dense
            maxlength="64"
            counter
            :label="t('admin.settings.instanceName')"
            :hint="t('admin.settings.instanceNameHint')"
            :rules="[(v: string) => Boolean(v.trim()) || t('admin.settings.instanceNameRequired')]"
            :error="Boolean(fieldError)"
            :error-message="fieldError ?? undefined"
            :loading="state.loading.value && !state.data.value"
            class="col"
          />
          <q-btn unelevated no-caps color="primary" type="submit" :label="t('common.save')" :disable="!dirty" :loading="saving" />
        </q-form>
        <div class="settings__meta text-faint">
          <template v-if="state.data.value?.updated_at">
            {{ t('admin.settings.lastChanged', { time: relativeTime(state.data.value.updated_at), user: state.data.value.updated_by ?? '–' }) }}
          </template>
          <template v-else>{{ t('admin.settings.neverChanged') }}</template>
        </div>
      </PageCard>

      <PageCard :title="t('admin.settings.twoFactor')" :caption="t('admin.settings.twoFactorCaption')">
        <ProblemBanner :error="policyProblem" />
        <q-toggle
          :model-value="state.data.value?.security.require_totp_for_admins ?? false"
          :disable="!state.data.value || savingPolicy"
          :label="t('admin.settings.requireTotp')"
          @update:model-value="savePolicy"
        />
        <div class="settings__hint text-faint">{{ t('admin.settings.requireTotpHint') }}</div>
      </PageCard>

      <DockerRoutingCard v-if="auth.can('repositories.manage')" />

      <div class="settings__pair">
        <PageCard :title="t('admin.settings.security')" :caption="t('admin.settings.securityCaption')">
          <LimitList :items="securityItems" />
        </PageCard>
        <PageCard :title="t('admin.settings.retention')" :caption="t('admin.settings.retentionCaption')">
          <LimitList :items="retentionItems" />
        </PageCard>
      </div>
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { EnvironmentLimits } from '@/api/types'
import DockerRoutingCard from '@/components/admin/DockerRoutingCard.vue'
import type { LimitItem } from '@/components/admin/forms'
import LimitList from '@/components/admin/LimitList.vue'
import ProblemBanner from '@/components/admin/ProblemBanner.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useConfigStore } from '@/stores/config'
import { formatBytes, relativeTime } from '@/utils/format'

const notify = useNotify()
const auth = useAuthStore()
const config = useConfigStore()
const router = useRouter()
const state = useAsync(() => call(api.GET('/settings')))

const instanceName = ref('')
watch(state.data, (value) => (instanceName.value = value?.general.instance_name ?? ''), { immediate: true })
const dirty = computed(() => Boolean(state.data.value) && instanceName.value.trim() !== state.data.value?.general.instance_name)

const saving = ref(false)
const problem = ref<unknown>(null)
const fieldError = ref<string | null>(null)

async function save(): Promise<void> {
  saving.value = true
  problem.value = null
  fieldError.value = null
  try {
    const saved = await call(api.PUT('/settings', { body: { general: { instance_name: instanceName.value.trim() } } }))
    state.data.value = saved
    if (config.config) config.config = { ...config.config, instance_name: saved.general.instance_name }
    notify.success(t('admin.settings.saved'))
  } catch (error) {
    if (error instanceof ApiError) fieldError.value = error.fieldErrors['general.instance_name'] ?? null
    problem.value = error
  } finally {
    saving.value = false
  }
}

const savingPolicy = ref(false)
const policyProblem = ref<unknown>(null)

/** Saved on toggle; the instance name sent is the stored one (an unsaved edit above stays unsaved). */
async function savePolicy(value: boolean): Promise<void> {
  const current = state.data.value
  if (!current) return
  savingPolicy.value = true
  policyProblem.value = null
  try {
    state.data.value = await call(api.PUT('/settings', { body: { general: current.general, security: { require_totp_for_admins: value } } }))
    notify.success(t(value ? 'admin.settings.requireTotpOn' : 'admin.settings.requireTotpOff'))
    if (value) {
      await auth.refresh() // the change may apply to the caller at once: then set it up first
      if (auth.totpSetupRequired) await router.push({ name: 'profile' })
    }
  } catch (error) {
    policyProblem.value = error
  } finally {
    savingPolicy.value = false
  }
}

type Key = keyof EnvironmentLimits
const value = (key: Key): number | null => state.data.value?.limits[key] ?? null
const minutes = (n: number | null) => (n === null ? '–' : t('admin.settings.minutes', { count: n }))
const hours = (n: number | null) => (n === null ? '–' : t('admin.settings.hours', { count: n }))
const days = (n: number | null) => (n === null ? '–' : t('admin.settings.days', { count: n }))
const plain = (n: number | null) => (n === null ? '–' : String(n))
const env = (key: Key) => `WAYSTATION_${key.toUpperCase()}`

function item(key: Key, text: (n: number | null) => string, envName: string | null = env(key)): LimitItem {
  return { key, label: t(`admin.settings.limit.${key}`), value: text(value(key)), env: envName }
}

const securityItems = computed<LimitItem[]>(() => [
  item('session_idle_minutes', minutes),
  item('session_max_hours', hours),
  item('login_rate_limit', plain),
  item('login_lockout_failures', plain),
  item('login_lockout_minutes', minutes),
  item('token_failure_limit', plain),
])
const retentionItems = computed<LimitItem[]>(() => [
  item('audit_retention_days', days),
  item('stats_minute_retention_days', days, null),
  item('max_upload_bytes', formatBytes, 'WAYSTATION_MAX_UPLOAD_GB'),
  item('min_free_bytes', formatBytes, 'WAYSTATION_MIN_FREE_GB'),
])
</script>

<style scoped>
.settings {
  display: grid;
  gap: 16px;
}

.settings__general {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 10px;
  max-width: 720px;
}

.settings__hint {
  margin: 4px 0 0 44px;
  font-size: var(--fs-12);
}

.settings__meta {
  margin-top: 8px;
  font-size: var(--fs-12);
}

.settings__pair {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 16px;
}

@media (max-width: 599px) {
  .settings__pair {
    grid-template-columns: 1fr;
  }
}
</style>
