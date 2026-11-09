<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A webhook's delivery log (side panel): status filter, newest first with "load more"; one delivery opens its
     request (headers, JSON body) and every attempt with the receiver's answer, and can be sent again. Refreshes
     every few seconds while a delivery is queued or retrying. -->
<template>
  <q-dialog v-model="open" position="right" full-height :maximized="$q.screen.lt.sm" transition-show="slide-left" transition-hide="slide-right">
    <q-card class="deliveries" :aria-label="title">
      <header class="deliveries__head">
        <q-btn v-if="selectedId" flat round dense :icon="iconBack" :aria-label="t('webhooks.deliveries.back')" @click="selectedId = null" />
        <div v-else class="deliveries__icon"><q-icon :name="iconWebhook" size="22px" /></div>
        <div class="col">
          <div class="deliveries__title">{{ title }}</div>
          <div class="text-caption">{{ t('webhooks.deliveries.caption') }}</div>
        </div>
        <q-btn flat round dense :icon="iconRefresh" :aria-label="t('webhooks.refresh')" :loading="list.loading.value || detail.loading.value" @click="refresh" />
        <q-btn v-close-popup flat round dense :icon="iconClose" :aria-label="t('common.close')" />
      </header>

      <!-- one delivery -->
      <div v-if="selectedId" class="deliveries__body">
        <div v-if="detail.error.value && !delivery" role="alert">{{ errorMessage(detail.error.value) }}</div>
        <q-skeleton v-else-if="!delivery" height="200px" />
        <template v-else>
          <div class="deliveries__status">
            <DeliveryStatusPill :status="delivery.status" :attempts="delivery.attempts" />
            <span class="ws-badge deliveries__trigger">{{ t(`webhooks.trigger.${delivery.trigger}`) }}</span>
            <q-space />
            <q-btn unelevated no-caps color="primary" :icon="iconRetry" :label="t('webhooks.action.redeliver')" :loading="redelivering" @click="redeliver(delivery.id)" />
          </div>
          <dl class="deliveries__facts">
            <dt>{{ t('webhooks.deliveries.event') }}</dt>
            <dd>{{ eventLabel(delivery.event) }} <span class="mono text-faint">{{ delivery.event }}</span></dd>
            <template v-if="delivery.repository">
              <dt>{{ t('webhooks.deliveries.repository') }}</dt>
              <dd>{{ delivery.repository }}</dd>
            </template>
            <dt>{{ t('webhooks.deliveries.created') }}</dt>
            <dd>{{ absoluteTime(delivery.created_at) }}</dd>
            <template v-if="delivery.next_attempt_at">
              <dt>{{ t('webhooks.status.retrying') }}</dt>
              <dd>{{ t('webhooks.deliveries.nextAttempt', { time: relativeTime(delivery.next_attempt_at, now) }) }}</dd>
            </template>
            <template v-if="delivery.finished_at">
              <dt>{{ t('webhooks.deliveries.finished') }}</dt>
              <dd>{{ absoluteTime(delivery.finished_at) }}</dd>
            </template>
            <dt>{{ t('webhooks.deliveries.deliveryId') }}</dt>
            <dd class="mono">{{ delivery.id }}</dd>
            <dt>{{ t('webhooks.deliveries.eventId') }}</dt>
            <dd class="mono">{{ delivery.event_id }}</dd>
            <template v-if="delivery.redelivery_of">
              <dt>{{ t('webhooks.deliveries.redeliveryOf') }}</dt>
              <dd class="mono">
                <a href="#" @click.prevent="selectedId = delivery.redelivery_of">{{ delivery.redelivery_of }}</a>
              </dd>
            </template>
          </dl>

          <section class="deliveries__section">
            <h3 class="deliveries__h">{{ t('webhooks.deliveries.attemptsTitle') }}</h3>
            <div v-if="!delivery.log.length" class="text-faint">{{ t('webhooks.deliveries.noAttempts') }}</div>
            <ol v-else class="deliveries__attempts">
              <li v-for="a in delivery.log" :key="a.n" class="deliveries__attempt" :class="a.outcome === 'succeeded' ? 'is-ok' : 'is-failed'">
                <div class="deliveries__attempt-head">
                  <q-icon :name="a.outcome === 'succeeded' ? iconOk : iconError" size="18px" />
                  <strong>{{ t('webhooks.deliveries.attempt', { n: a.n }) }}</strong>
                  <span class="num">{{ deliveryResult({ ...a, attempts: 1 }) }}</span>
                  <q-space />
                  <TimeCell :value="a.at" class="text-faint" />
                </div>
                <pre v-if="a.response" class="deliveries__pre deliveries__pre--small" :aria-label="t('webhooks.deliveries.response')">{{ a.response }}</pre>
              </li>
            </ol>
          </section>

          <section class="deliveries__section">
            <h3 class="deliveries__h">{{ t('webhooks.deliveries.request') }}</h3>
            <dl class="deliveries__headers mono">
              <template v-for="(value, key) in delivery.request_headers" :key="key">
                <dt>{{ key }}:</dt>
                <dd>{{ value }}</dd>
              </template>
            </dl>
            <pre class="deliveries__pre">{{ prettyJson(delivery.request_body) }}</pre>
          </section>
        </template>
      </div>

      <!-- the log -->
      <div v-else class="deliveries__body deliveries__body--list">
        <q-tabs v-model="status" dense no-caps align="left" class="deliveries__tabs" active-color="primary" indicator-color="primary">
          <q-tab name="all" :label="t('webhooks.deliveries.all')" />
          <q-tab name="pending" :label="t('webhooks.status.pending') + ' / ' + t('webhooks.status.retrying')" />
          <q-tab name="dead" :label="t('webhooks.status.dead')" />
          <q-tab name="succeeded" :label="t('webhooks.status.succeeded')" />
        </q-tabs>
        <div v-if="list.error.value && !items.length" class="deliveries__empty" role="alert">{{ t('webhooks.deliveries.loadFailed') }}: {{ errorMessage(list.error.value) }}</div>
        <q-skeleton v-else-if="list.loading.value && !list.data.value" height="160px" class="q-ma-md" />
        <EmptyState v-else-if="!items.length" class="deliveries__empty" illustration="activity" :title="t('webhooks.deliveries.empty')" :text="t('webhooks.deliveries.emptyText')" />
        <q-list v-else separator class="deliveries__list">
          <q-item v-for="d in items" :key="d.id" clickable @click="selectedId = d.id">
            <q-item-section>
              <q-item-label class="deliveries__row">
                <DeliveryStatusPill :status="d.status" :attempts="d.attempts" :tooltip="false" />
                <span class="deliveries__event">{{ eventLabel(d.event) }}</span>
                <span v-if="d.repository" class="text-faint">{{ d.repository }}</span>
              </q-item-label>
              <q-item-label caption>
                {{ deliveryResult(d) }}
                <template v-if="d.attempts > 1"> · {{ t('webhooks.deliveries.attempts', { count: d.attempts }) }}</template>
                <template v-if="d.next_attempt_at"> · {{ t('webhooks.deliveries.nextAttempt', { time: relativeTime(d.next_attempt_at, now) }) }}</template>
              </q-item-label>
            </q-item-section>
            <q-item-section side top>
              <TimeCell :value="d.created_at" />
              <span v-if="d.trigger !== 'event'" class="ws-badge deliveries__trigger">{{ t(`webhooks.trigger.${d.trigger}`) }}</span>
            </q-item-section>
          </q-item>
        </q-list>
        <div v-if="nextCursor" class="deliveries__more">
          <q-btn flat no-caps color="primary" :label="t('webhooks.deliveries.loadMore')" :loading="loadingMore" @click="loadMore" />
        </div>
      </div>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { Webhook, WebhookDeliveryStatus, WebhookDeliverySummary } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { useNow } from '@/composables/useNow'
import { usePolling } from '@/composables/usePolling'
import { t } from '@/i18n'
import { absoluteTime, relativeTime } from '@/utils/format'
import { iconBack, iconClose, iconError, iconOk, iconRefresh, iconRetry, iconWebhook } from '@/utils/icons'

import DeliveryStatusPill from './DeliveryStatusPill.vue'
import { deliveryResult, eventLabel, prettyJson } from './webhooks'

const props = defineProps<{ webhook: Webhook | null; focus?: string | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ changed: [] }>()
const $q = useQuasar()
const notify = useNotify()
const now = useNow()

const status = ref<'all' | WebhookDeliveryStatus>('all')
const selectedId = ref<string | null>(null)
const extra = ref<WebhookDeliverySummary[]>([])
const nextCursor = ref<string | null>(null)
const loadingMore = ref(false)
const redelivering = ref(false)

const title = computed(() => t('webhooks.deliveries.title', { name: props.webhook?.name ?? '' }))

async function page(cursor: string | null) {
  if (!props.webhook) return null
  return call(
    api.GET('/webhooks/{webhook_id}/deliveries', {
      params: { path: { webhook_id: props.webhook.id }, query: { status: status.value === 'all' ? undefined : status.value, cursor: cursor ?? undefined, limit: 50 } },
    }),
  )
}

const list = useAsync(() => (open.value ? page(null) : Promise.resolve(null)), [open, () => props.webhook?.id, status])
watch(list.data, (value) => {
  extra.value = []
  nextCursor.value = value?.next_cursor ?? null
})
const items = computed(() => [...(list.data.value?.items ?? []), ...extra.value])

const detail = useAsync(
  async () => (open.value && props.webhook && selectedId.value ? call(api.GET('/webhooks/{webhook_id}/deliveries/{delivery_id}', { params: { path: { webhook_id: props.webhook.id, delivery_id: selectedId.value } } })) : null),
  [open, selectedId],
)
const delivery = computed(() => (detail.data.value?.id === selectedId.value ? detail.data.value : null))

watch(open, (isOpen) => {
  if (isOpen) {
    status.value = 'all'
    selectedId.value = props.focus ?? null
  }
})
watch(
  () => props.focus,
  (id) => {
    if (open.value && id) selectedId.value = id
  },
)

const busy = computed(() => open.value && (selectedId.value ? delivery.value?.status === 'pending' : items.value.some((d) => d.status === 'pending')))
usePolling(busy, () => refresh(), 2500)

async function refresh(): Promise<void> {
  await (selectedId.value ? detail.reload() : list.reload())
}

async function loadMore(): Promise<void> {
  loadingMore.value = true
  try {
    const next = await page(nextCursor.value)
    if (next) {
      extra.value = [...extra.value, ...next.items]
      nextCursor.value = next.next_cursor
    }
  } catch (error) {
    notify.error(error)
  } finally {
    loadingMore.value = false
  }
}

async function redeliver(deliveryId: string): Promise<void> {
  if (!props.webhook) return
  redelivering.value = true
  try {
    const accepted = await call(api.POST('/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver', { params: { path: { webhook_id: props.webhook.id, delivery_id: deliveryId } } }))
    notify.success(t('webhooks.redelivered'))
    selectedId.value = accepted.delivery_id
    void list.reload()
    emit('changed')
  } catch (error) {
    notify.error(error)
  } finally {
    redelivering.value = false
  }
}
</script>

<style scoped>
.deliveries {
  width: 640px;
  max-width: 100vw;
  height: 100%;
  display: flex;
  flex-direction: column;
}

.deliveries__head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
}

.deliveries__icon {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: var(--radius-sm);
  background: var(--surface-brand);
  color: var(--q-primary);
}

.deliveries__title {
  font-weight: 600;
  font-size: var(--fs-16);
}

.deliveries__body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px 24px;
}

.deliveries__body--list {
  padding: 0;
}

.deliveries__tabs {
  border-bottom: 1px solid var(--border);
  padding: 0 8px;
}

.deliveries__row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.deliveries__event {
  font-weight: 600;
}

.deliveries__trigger {
  color: var(--text-secondary);
  box-shadow: inset 0 0 0 1px var(--border);
}

.deliveries__empty {
  padding: 24px 20px;
}

.deliveries__more {
  display: flex;
  justify-content: center;
  padding: 12px;
}

.deliveries__status {
  display: flex;
  align-items: center;
  gap: 8px;
}

.deliveries__facts {
  display: grid;
  grid-template-columns: minmax(100px, max-content) 1fr;
  gap: 8px 16px;
  margin: 16px 0 0;
  font-size: var(--fs-13);
}

.deliveries__facts dt {
  color: var(--text-secondary);
}

.deliveries__facts dd {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
}

.deliveries__section {
  margin-top: 24px;
}

.deliveries__h {
  margin: 0 0 8px;
  font-size: var(--fs-14);
  font-weight: 600;
  line-height: 1.4;
}

.deliveries__attempts {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.deliveries__attempt {
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--fs-13);
}

.deliveries__attempt-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.deliveries__attempt.is-ok .q-icon {
  color: var(--status-ok);
}

.deliveries__attempt.is-failed .q-icon {
  color: var(--status-blocked);
}

.deliveries__headers {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 2px 8px;
  margin: 0 0 8px;
  font-size: var(--fs-12);
}

.deliveries__headers dt {
  color: var(--text-secondary);
}

.deliveries__headers dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.deliveries__pre {
  margin: 0;
  max-height: 360px;
  overflow: auto;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--surface-sunken, var(--surface-brand));
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-12);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.deliveries__pre--small {
  margin-top: 8px;
  max-height: 160px;
}
</style>
