<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Administration → Provisioning (T-O09): the configured provisioning file, how its last apply went, the plan (what
     applying it now would change, field by field; secrets only as "secret changed") and Apply. The file itself is
     edited where it lives (GitOps); this page never uploads one. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.provisioning')" :caption="t('provisioning.caption')">
      <template v-if="configured" #actions>
        <q-btn outline no-caps :icon="iconRefresh" :label="t('provisioning.makePlan')" :loading="planning" @click="makePlan" />
        <q-btn unelevated no-caps color="primary" :icon="iconPlay" :label="t('provisioning.apply')" :loading="applying" :disable="!canApply" @click="confirmApply" />
      </template>
    </PageHeader>

    <EmptyState v-if="!isAdmin" illustration="error" :title="t('admin.problem.forbidden')" :text="t('provisioning.forbidden')" />
    <EmptyState v-else-if="status.error.value && !status.data.value" illustration="error" :title="t('provisioning.loadFailed')" :text="errorMessage(status.error.value)">
      <q-btn unelevated no-caps color="primary" :label="t('common.retry')" @click="status.reload" />
    </EmptyState>
    <q-skeleton v-else-if="!status.data.value" height="140px" />
    <PageCard v-else-if="!configured">
      <EmptyState illustration="soon" :title="t('provisioning.notConfiguredTitle')" :text="t('provisioning.notConfiguredText')">
        <div class="text-faint">{{ t('provisioning.docsHint') }}</div>
      </EmptyState>
    </PageCard>

    <div v-else class="prov">
      <div class="prov__facts">
        <div class="prov__fact q-card">
          <div class="prov__label">{{ t('provisioning.file') }}</div>
          <div class="prov__value mono">{{ status.data.value.file }}</div>
        </div>
        <div class="prov__fact q-card">
          <div class="prov__label">{{ t('provisioning.lastApply') }}</div>
          <div v-if="last" class="prov__value">
            <span class="ws-badge" :class="last.outcome === 'applied' ? 'tone-ok' : 'tone-blocked'">{{ t(`provisioning.outcome.${last.outcome}`) }}</span>
            <TimeCell :value="last.at" />
            <span class="text-muted">{{ t('provisioning.by', { who: last.by }) }}</span>
            <span v-if="last.sha256" class="mono text-faint" :title="last.sha256">{{ last.sha256.slice(0, 12) }}</span>
          </div>
          <div v-else class="prov__value text-faint">{{ t('provisioning.never') }}</div>
        </div>
      </div>

      <PageCard :title="t('provisioning.plan')" :caption="t('provisioning.planCaption')" flush>
        <template v-if="plan" #actions>
          <div class="prov__summary">
            <span v-for="key in SUMMARY_KEYS" v-show="(plan.summary?.[key] ?? 0) > 0 || key === 'unchanged'" :key="key" class="prov__count">
              {{ t(`provisioning.summary.${key}`, { count: plan.summary?.[key] ?? 0 }) }}
            </span>
          </div>
        </template>
        <q-skeleton v-if="planning && !plan" height="120px" class="q-ma-md" />
        <div v-else-if="planError" class="prov__error" role="alert">{{ errorMessage(planError) }}</div>
        <template v-else-if="plan">
          <div v-if="!plan.valid" class="prov__invalid" role="alert">
            <div class="prov__invalid-title"><q-icon :name="iconError" size="18px" /> {{ t('provisioning.invalidTitle') }}</div>
            <ul>
              <li v-for="(e, i) in plan.errors ?? []" :key="i"><span class="mono">{{ e.field }}</span>: {{ e.message }}</li>
            </ul>
          </div>
          <EmptyState v-else-if="changeCount(plan) === 0 && !showUnchanged" illustration="activity" :title="t('provisioning.noChanges')" :text="t('provisioning.noChangesText')" />
          <ul v-if="shown.length" class="prov__items">
            <li v-for="item in shown" :key="`${item.kind}:${item.name}`" class="prov__item">
              <div class="prov__item-head">
                <span class="ws-badge" :class="ACTION_TONE[item.action]">{{ t(`provisioning.action.${item.action}`) }}</span>
                <span class="prov__kind">{{ t(`provisioning.kind.${item.kind}`) }}</span>
                <span class="prov__name">{{ item.name }}</span>
                <span v-if="item.outcome" class="ws-badge" :class="item.outcome === 'applied' ? 'tone-ok' : 'tone-blocked'">{{ t(`provisioning.itemOutcome.${item.outcome}`) }}</span>
              </div>
              <div v-if="item.note" class="prov__note text-muted">{{ item.note }}</div>
              <div v-if="item.error" class="prov__note text-negative">{{ item.error }}</div>
              <ul v-if="item.action !== 'unchanged' && item.changes?.length" class="prov__changes mono">
                <li v-for="c in item.changes" :key="c.field" :class="{ 'prov__change--secret': c.redacted }">{{ describeChange(c) }}</li>
              </ul>
            </li>
          </ul>
          <div v-if="(plan.summary?.unchanged ?? 0) > 0" class="prov__toggle">
            <q-toggle v-model="showUnchanged" dense :label="t('provisioning.showUnchanged', { count: plan.summary?.unchanged ?? 0 })" />
          </div>
        </template>
      </PageCard>
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { ProvisioningPlan } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import { ACTION_TONE, changeCount, describeChange, visibleItems } from '@/components/provisioning/provisioning'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { iconError, iconPlay, iconRefresh } from '@/utils/icons'

const SUMMARY_KEYS = ['create', 'update', 'prune', 'unchanged', 'failed'] as const

const $q = useQuasar()
const notify = useNotify()
const auth = useAuthStore()
const isAdmin = computed(() => auth.can('admin'))

const status = useAsync(() => (isAdmin.value ? call(api.GET('/provisioning')) : Promise.resolve(null)))
const configured = computed(() => Boolean(status.data.value?.configured))
const last = computed(() => status.data.value?.last_apply ?? null)

const plan = ref<ProvisioningPlan | null>(null)
const planError = ref<unknown>(null)
const planning = ref(false)
const applying = ref(false)
const showUnchanged = ref(false)
const shown = computed(() => visibleItems(plan.value?.items ?? [], showUnchanged.value))
const canApply = computed(() => Boolean(plan.value?.valid) && changeCount(plan.value) > 0 && !planning.value)

async function makePlan(): Promise<void> {
  planning.value = true
  planError.value = null
  try {
    plan.value = await call(api.POST('/provisioning/plan'))
  } catch (error) {
    planError.value = error
  } finally {
    planning.value = false
  }
}

watch(configured, (yes) => {
  if (yes && !plan.value) void makePlan()
}, { immediate: true })

function confirmApply(): void {
  $q.dialog({
    title: t('provisioning.applyTitle'),
    message: t('provisioning.applyText', { count: changeCount(plan.value) }),
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'primary', label: t('provisioning.apply') },
    persistent: true,
  }).onOk(apply)
}

async function apply(): Promise<void> {
  applying.value = true
  try {
    const result = await call(api.POST('/provisioning/apply'))
    plan.value = result
    const failed = result.summary?.failed ?? 0
    if (failed) notify.error(t('provisioning.appliedWithFailures', { failed }))
    else notify.success(t('provisioning.applied', { count: changeCount(result) }))
    await status.reload()
  } catch (error) {
    notify.error(error)
  } finally {
    applying.value = false
  }
}
</script>

<style scoped>
.prov {
  display: grid;
  gap: 16px;
}

.prov__facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px;
}

.prov__fact {
  padding: 14px 16px;
}

.prov__label {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 6px;
}

.prov__value {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  overflow-wrap: anywhere;
}

.prov__summary {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 13px;
  color: var(--text-secondary);
}

.prov__items {
  list-style: none;
  margin: 0;
  padding: 0;
}

.prov__item {
  padding: 12px 16px;
  border-top: 1px solid var(--border);
}

.prov__item-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.prov__kind {
  color: var(--text-muted);
  font-size: 13px;
}

.prov__name {
  font-weight: 600;
}

.prov__note {
  margin-top: 4px;
  font-size: 13px;
}

.prov__changes {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.prov__change--secret {
  color: var(--text-muted);
  font-style: italic;
}

.prov__invalid,
.prov__error {
  margin: 12px 16px;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  color: var(--status-blocked);
  background: var(--status-blocked-bg);
}

.prov__invalid-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}

.prov__invalid ul {
  margin: 8px 0 0;
  padding-left: 20px;
}

.prov__toggle {
  padding: 10px 16px;
  border-top: 1px solid var(--border);
}
</style>
