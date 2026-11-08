<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Audit record detail (side panel): who, what, where from, failure reason, details and the field diff. Secret
     fields never carry values; they show as "secret changed". All text is rendered as text (never HTML). -->
<template>
  <q-dialog v-model="open" position="right" full-height :maximized="$q.screen.lt.sm" transition-show="slide-left" transition-hide="slide-right">
    <q-card v-if="record" class="audit-drawer" :aria-label="t('audit.drawer.title')">
      <header class="audit-drawer__head">
        <div class="audit-drawer__icon"><q-icon :name="auditCategoryIcon(record.action)" size="22px" /></div>
        <div class="col">
          <div class="audit-drawer__title">{{ auditActionLabel(record.action, record.outcome) }}</div>
          <div class="text-caption mono">{{ record.action }}</div>
        </div>
        <q-btn v-close-popup flat round dense :icon="iconClose" :aria-label="t('common.close')" />
      </header>

      <div class="audit-drawer__body">
        <AuditOutcomePill :outcome="record.outcome" :reason="record.reason" inline />

        <dl class="audit-drawer__facts">
          <dt>{{ t('audit.drawer.when') }}</dt>
          <dd>{{ absoluteTime(record.ts) }} <span class="text-faint">· <TimeCell :value="record.ts" /></span></dd>
          <dt>{{ t('audit.drawer.actor') }}</dt>
          <dd>{{ record.actor.name }} <span v-if="record.actor.id" class="mono text-faint">{{ record.actor.id }}</span></dd>
          <dt>{{ t('audit.drawer.auth') }}</dt>
          <dd>{{ auditAuthLabel(record.actor.auth) }}</dd>
          <template v-if="record.actor.provider">
            <dt>{{ t('audit.drawer.provider') }}</dt>
            <dd class="mono">{{ record.actor.provider }}</dd>
          </template>
          <template v-if="record.actor.token_id">
            <dt>{{ t('audit.drawer.token') }}</dt>
            <dd class="mono">{{ record.actor.token_id }}</dd>
          </template>
          <template v-if="record.target">
            <dt>{{ t('audit.drawer.target') }}</dt>
            <dd>
              {{ auditTargetKindLabel(record.target.kind) }} · {{ record.target.name ?? '–' }}
              <span v-if="record.target.id && record.target.id !== record.target.name" class="mono text-faint">{{ record.target.id }}</span>
            </dd>
          </template>
          <template v-if="record.reason">
            <dt>{{ t('audit.drawer.reason') }}</dt>
            <dd>{{ auditReasonLabel(record.reason) }} <span class="mono text-faint">{{ record.reason }}</span></dd>
          </template>
          <dt>{{ t('audit.drawer.client') }}</dt>
          <dd class="mono">{{ record.client_ip ?? '–' }}</dd>
          <template v-if="record.request_id">
            <dt>{{ t('audit.drawer.requestId') }}</dt>
            <dd class="audit-drawer__copy">
              <span class="mono">{{ record.request_id }}</span>
              <q-btn flat dense round size="sm" :icon="iconCopy" :aria-label="t('common.copyWhat', { what: t('audit.drawer.requestId') })" @click="copy(record.request_id, t('audit.drawer.requestId'))" />
            </dd>
          </template>
          <dt>{{ t('audit.drawer.recordId') }}</dt>
          <dd class="mono text-faint">{{ record.id }}</dd>
        </dl>

        <section v-if="details.length" class="audit-drawer__section">
          <h2 class="audit-drawer__h">{{ t('audit.drawer.details') }}</h2>
          <dl class="audit-drawer__facts">
            <template v-for="[key, value] in details" :key="key">
              <dt class="mono">{{ key }}</dt>
              <dd class="mono audit-drawer__value">{{ auditValue(value) }}</dd>
            </template>
          </dl>
        </section>

        <section class="audit-drawer__section">
          <h2 class="audit-drawer__h">{{ t('audit.drawer.changes') }}</h2>
          <div v-if="!record.changes.length" class="text-faint">{{ t('audit.drawer.noChanges') }}</div>
          <table v-else class="audit-diff">
            <thead>
              <tr>
                <th>{{ t('audit.drawer.field') }}</th>
                <th>{{ t('audit.drawer.before') }}</th>
                <th>{{ t('audit.drawer.after') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in record.changes" :key="c.field">
                <td class="mono audit-diff__field">{{ c.field }}</td>
                <td v-if="c.redacted" colspan="2" class="audit-diff__secret">
                  <q-icon :name="iconLock" size="16px" />{{ t('audit.drawer.secretChanged') }}
                </td>
                <template v-else>
                  <td class="mono audit-diff__before">{{ auditValue(c.before) }}</td>
                  <td class="mono audit-diff__after">{{ auditValue(c.after) }}</td>
                </template>
              </tr>
            </tbody>
          </table>
        </section>
      </div>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { AuditRecord } from '@/api/types'
import AuditOutcomePill from '@/components/audit/AuditOutcomePill.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useCopy } from '@/composables/useNotify'
import { t } from '@/i18n'
import { auditActionLabel, auditAuthLabel, auditCategoryIcon, auditReasonLabel, auditTargetKindLabel, auditValue } from '@/utils/audit'
import { absoluteTime } from '@/utils/format'
import { iconClose, iconCopy, iconLock } from '@/utils/icons'

const props = defineProps<{ record: AuditRecord | null }>()
const open = defineModel<boolean>({ required: true })
const copy = useCopy()

const details = computed(() => Object.entries(props.record?.details ?? {}))
</script>

<style scoped>
.audit-drawer {
  width: 520px;
  max-width: 100vw;
  height: 100%;
  display: flex;
  flex-direction: column;
}

.audit-drawer__head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
}

.audit-drawer__icon {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: var(--radius-sm);
  background: var(--surface-brand);
  color: var(--q-primary);
}

.audit-drawer__title {
  font-weight: 600;
  font-size: var(--fs-16);
}

.audit-drawer__body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px 24px;
}

.audit-drawer__facts {
  display: grid;
  grid-template-columns: minmax(110px, max-content) 1fr;
  gap: 8px 16px;
  margin: 16px 0 0;
  font-size: var(--fs-13);
}

.audit-drawer__facts dt {
  color: var(--text-secondary);
}

.audit-drawer__facts dd {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
}

.audit-drawer__copy {
  display: flex;
  align-items: center;
  gap: 4px;
}

.audit-drawer__value {
  white-space: pre-wrap;
}

.audit-drawer__section {
  margin-top: 24px;
}

.audit-drawer__h {
  margin: 0 0 8px;
  font-size: var(--fs-14);
  font-weight: 600;
  line-height: 1.4;
}

.audit-diff {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-12);
  table-layout: fixed;
}

.audit-diff th {
  text-align: left;
  font-weight: 600;
  color: var(--text-secondary);
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
}

.audit-diff td {
  padding: 6px 8px;
  vertical-align: top;
  border-bottom: 1px solid var(--border);
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.audit-diff__field {
  color: var(--text-secondary);
}

.audit-diff__before {
  color: var(--cache-error);
  background: var(--cache-error-bg);
}

.audit-diff__after {
  color: var(--status-ok);
  background: var(--status-ok-bg);
}

.audit-diff__secret {
  color: var(--text-secondary);
}

.audit-diff__secret .q-icon {
  margin-right: 6px;
  vertical-align: -3px;
}
</style>
