<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Administration → Webhooks (T-O05, UI_DESIGN §4.15): the webhooks with their last delivery, the editor, a test
     event (its delivery opens in the log), the delivery log with redelivery. `?edit=<id>` / `?new=1` open the editor. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.webhooks')" :caption="t('webhooks.caption')">
      <template #actions>
        <q-btn v-if="isAdmin" unelevated no-caps color="primary" :icon="iconAdd" :label="t('webhooks.newWebhook')" @click="edit(null)" />
      </template>
    </PageHeader>

    <EmptyState v-if="!isAdmin" illustration="error" :title="t('admin.problem.forbidden')" :text="t('webhooks.forbidden')" />
    <PageCard v-else :title="t('webhooks.list')" :caption="t('webhooks.listCaption')" flush>
      <EmptyState v-if="list.error.value && !list.data.value" illustration="error" :title="t('webhooks.loadFailed')" :text="errorMessage(list.error.value)">
        <q-btn unelevated no-caps color="primary" :label="t('common.retry')" :loading="list.loading.value" @click="list.reload" />
      </EmptyState>
      <WebhookTable
        v-else
        :webhooks="webhooks"
        :loading="list.loading.value && !list.data.value"
        :testing="testing"
        @edit="edit"
        @test="test"
        @deliveries="(w: Webhook) => showDeliveries(w)"
        @delete="confirmDelete"
        @toggle="toggle"
      >
        <template #empty>
          <EmptyState class="full-width" illustration="activity" :title="t('webhooks.emptyTitle')" :text="t('webhooks.emptyText')">
            <q-btn unelevated no-caps color="primary" :icon="iconAdd" :label="t('webhooks.newWebhook')" @click="edit(null)" />
          </EmptyState>
        </template>
      </WebhookTable>
    </PageCard>

    <WebhookEditor v-model="editorOpen" :webhook="editing" @saved="list.reload" />
    <DeliveriesDrawer v-model="deliveriesOpen" :webhook="deliveriesFor" :focus="focusDelivery" @changed="list.reload" />
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, call, errorMessage } from '@/api/client'
import type { Webhook } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import DeliveriesDrawer from '@/components/webhooks/DeliveriesDrawer.vue'
import WebhookEditor from '@/components/webhooks/WebhookEditor.vue'
import { toWrite } from '@/components/webhooks/webhooks'
import WebhookTable from '@/components/webhooks/WebhookTable.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { usePolling } from '@/composables/usePolling'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { iconAdd } from '@/utils/icons'

const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const notify = useNotify()
const auth = useAuthStore()
const isAdmin = computed(() => auth.can('admin'))

const list = useAsync(() => (isAdmin.value ? call(api.GET('/webhooks')) : Promise.resolve(null)))
const webhooks = computed(() => list.data.value?.items ?? [])
usePolling(() => webhooks.value.some((w) => w.last_delivery?.status === 'pending'), () => list.reload(), 3000)

const editorOpen = ref(false)
const editing = ref<Webhook | null>(null)
const deliveriesOpen = ref(false)
const deliveriesFor = ref<Webhook | null>(null)
const focusDelivery = ref<string | null>(null)
const testing = ref<string | null>(null)

function edit(hook: Webhook | null): void {
  editing.value = hook
  editorOpen.value = true
}

function showDeliveries(hook: Webhook, focus: string | null = null): void {
  deliveriesFor.value = hook
  focusDelivery.value = focus
  deliveriesOpen.value = true
}

watch(
  () => [list.data.value, route.query.edit, route.query.new] as const,
  ([data, id, isNew]) => {
    if (!data) return
    const target = typeof id === 'string' ? data.items.find((w) => w.id === id) : undefined
    if (target || isNew === '1') {
      edit(target ?? null)
      void router.replace({ query: {} })
    }
  },
  { immediate: true },
)

async function test(hook: Webhook): Promise<void> {
  testing.value = hook.id
  try {
    const accepted = await call(api.POST('/webhooks/{webhook_id}/test', { params: { path: { webhook_id: hook.id } } }))
    notify.success(t('webhooks.testSent', { name: hook.name }))
    showDeliveries(hook, accepted.delivery_id)
    await list.reload()
  } catch (error) {
    notify.error(error)
  } finally {
    testing.value = null
  }
}

async function toggle(hook: Webhook, enabled: boolean): Promise<void> {
  try {
    await call(api.PUT('/webhooks/{webhook_id}', { params: { path: { webhook_id: hook.id } }, body: toWrite(hook, { enabled }) }))
    notify.success(enabled ? t('webhooks.enabledToast', { name: hook.name }) : t('webhooks.pausedToast', { name: hook.name }))
    await list.reload()
  } catch (error) {
    notify.error(error)
  }
}

function confirmDelete(hook: Webhook): void {
  $q.dialog({
    title: t('webhooks.deleteTitle', { name: hook.name }),
    message: t('webhooks.deleteText'),
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'negative', label: t('webhooks.action.delete') },
    persistent: true,
  }).onOk(async () => {
    try {
      await call(api.DELETE('/webhooks/{webhook_id}', { params: { path: { webhook_id: hook.id } } }))
      notify.success(t('webhooks.deleted', { name: hook.name }))
      await list.reload()
    } catch (error) {
      notify.error(error)
    }
  })
}
</script>
