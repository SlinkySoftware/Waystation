<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Create or edit a webhook: target, events, repository scope, signing secret (write-only: generated in the browser,
     never shown again), custom headers (write-only values), delivery limits and the SSRF exception. Field errors come
     from problem+json. -->
<template>
  <FormDialog
    v-model="open"
    :title="webhook ? t('webhooks.editor.editTitle', { name: webhook.name }) : t('webhooks.editor.createTitle')"
    :caption="t('webhooks.editor.caption')"
    :saving="saving"
    :submit-label="webhook ? t('common.save') : t('webhooks.editor.create')"
    width="760px"
    @submit="save"
  >
    <div v-if="formError" class="hook-editor__error" role="alert">{{ formError }}</div>
    <div class="hook-editor">
      <div class="hook-editor__row">
        <q-input
          v-model="name"
          outlined
          dense
          autofocus
          maxlength="63"
          :label="t('webhooks.editor.name')"
          :hint="t('webhooks.editor.nameHint')"
          :rules="[(v: string) => Boolean(v?.trim()) || t('webhooks.editor.nameRequired')]"
          :error="Boolean(errors.name)"
          :error-message="errors.name"
        />
        <div class="hook-editor__enabled">
          <q-toggle v-model="enabled" :label="t('webhooks.editor.enabled')" />
          <div class="hook-editor__hint">{{ t('webhooks.editor.enabledHint') }}</div>
        </div>
      </div>
      <q-input
        v-model="url"
        outlined
        dense
        maxlength="2048"
        input-class="mono"
        :label="t('webhooks.editor.url')"
        :placeholder="t('webhooks.editor.urlHint')"
        :rules="[(v: string) => urlProblem(v ?? '') ?? true]"
        :error="Boolean(errors.url)"
        :error-message="errors.url"
      />
      <q-input v-model="description" outlined dense autogrow maxlength="500" :label="t('webhooks.editor.description')" :error="Boolean(errors.description)" :error-message="errors.description" />

      <section class="hook-editor__section">
        <div class="hook-editor__title">{{ t('webhooks.editor.events') }}</div>
        <div class="hook-editor__hint">{{ t('webhooks.editor.eventsCaption') }}</div>
        <q-toggle v-model="everything" :label="t('webhooks.editor.everything')" />
        <div v-if="!everything" class="hook-editor__events">
          <fieldset v-for="group in EVENT_GROUPS" :key="group.category" class="hook-editor__group">
            <legend>{{ t(`webhooks.category.${group.category}`) }}</legend>
            <q-checkbox v-for="event in group.events" :key="event" v-model="selected" :val="event" dense class="hook-editor__event">
              <span>{{ eventLabel(event) }}</span>
              <span class="hook-editor__event-help">{{ t(`webhooks.eventHelp.${event.replace('.', '_')}` as MessageKey) }}</span>
            </q-checkbox>
          </fieldset>
        </div>
        <div v-if="eventsError" class="hook-editor__need" role="alert">{{ eventsError }}</div>
      </section>

      <section class="hook-editor__section">
        <div class="hook-editor__title">{{ t('webhooks.editor.repositories') }}</div>
        <q-select
          v-model="repositories"
          outlined
          dense
          multiple
          use-chips
          use-input
          hide-dropdown-icon
          new-value-mode="add-unique"
          input-debounce="0"
          :options="repoOptions"
          :label="t('webhooks.editor.repositories')"
          :hint="t('webhooks.editor.repositoriesHint')"
          :error="Boolean(errors.repositories)"
          :error-message="errors.repositories"
          @filter="filterRepos"
        />
      </section>

      <section class="hook-editor__section">
        <div class="hook-editor__title">{{ t('webhooks.editor.secret') }}</div>
        <q-input
          v-model="secret"
          outlined
          dense
          :type="showSecret ? 'text' : 'password'"
          maxlength="256"
          autocomplete="new-password"
          :input-class="showSecret ? 'mono' : undefined"
          :label="t('webhooks.editor.secret')"
          :hint="webhook ? t('webhooks.editor.secretKeep') : t('webhooks.editor.secretHint', { min: SECRET_MIN })"
          :rules="[secretRule]"
          :error="Boolean(errors.secret)"
          :error-message="errors.secret"
        >
          <template #append>
            <q-btn flat round dense size="sm" :icon="showSecret ? iconHide : iconShow" :aria-label="showSecret ? t('signIn.hidePassword') : t('signIn.showPassword')" @click="showSecret = !showSecret" />
            <q-btn v-if="secret" flat round dense size="sm" :icon="iconCopy" :aria-label="t('webhooks.editor.copy')" @click="copy(secret, t('webhooks.editor.secret'))" />
          </template>
          <template #after>
            <q-btn flat no-caps dense :icon="iconRefresh" :label="t('webhooks.editor.generate')" @click="generate" />
          </template>
        </q-input>
        <details class="hook-editor__verify">
          <summary>{{ t('webhooks.editor.verifyTitle') }}</summary>
          <p>{{ t('webhooks.editor.verifyText') }}</p>
        </details>
      </section>

      <section class="hook-editor__section">
        <div class="hook-editor__title">{{ t('webhooks.editor.headers') }}</div>
        <div class="hook-editor__hint">{{ t('webhooks.editor.headersCaption') }}</div>
        <div v-if="webhook && !replaceHeaders" class="hook-editor__stored">
          <span>{{ webhook.header_names.length ? t('webhooks.editor.headersStored', { names: webhook.header_names.join(', ') }) : t('webhooks.editor.headersNone') }}</span>
          <q-btn flat dense no-caps color="primary" :label="t('webhooks.editor.replaceHeaders')" @click="startReplace" />
        </div>
        <template v-else>
          <div v-for="(row, i) in headerRows" :key="i" class="hook-editor__header">
            <q-input v-model="row.name" outlined dense maxlength="64" input-class="mono" :label="t('webhooks.editor.headerKey')" :rules="[() => rowProblem(row) ?? true]" :error="Boolean(errors[`headers.${row.name.trim()}`])" :error-message="errors[`headers.${row.name.trim()}`]" />
            <q-input v-model="row.value" outlined dense maxlength="1024" type="password" autocomplete="off" :label="t('webhooks.editor.headerValue')" />
            <q-btn flat round dense :icon="iconDelete" :aria-label="t('webhooks.editor.removeHeader')" @click="headerRows.splice(i, 1)" />
          </div>
          <div class="hook-editor__header-actions">
            <q-btn flat dense no-caps color="primary" :icon="iconAdd" :label="t('webhooks.editor.addHeader')" :disable="headerRows.length >= 20" @click="headerRows.push({ name: '', value: '' })" />
            <q-btn v-if="webhook" flat dense no-caps :label="t('webhooks.editor.keepHeaders')" @click="replaceHeaders = false" />
          </div>
        </template>
      </section>

      <section class="hook-editor__section">
        <div class="hook-editor__title">{{ t('webhooks.editor.delivery') }}</div>
        <div class="hook-editor__row hook-editor__row--even">
          <q-input v-model.number="timeoutS" outlined dense type="number" min="1" max="30" :label="t('webhooks.editor.timeout')" :rules="[(v: number) => (Number.isInteger(v) && v >= 1 && v <= 30) || '1–30']" :error="Boolean(errors.timeout_s)" :error-message="errors.timeout_s" />
          <q-input v-model.number="maxAttempts" outlined dense type="number" min="1" max="20" :label="t('webhooks.editor.maxAttempts')" :hint="t('webhooks.editor.maxAttemptsHint')" :rules="[(v: number) => (Number.isInteger(v) && v >= 1 && v <= 20) || '1–20']" :error="Boolean(errors.max_attempts)" :error-message="errors.max_attempts" />
        </div>
        <q-toggle v-model="allowPrivate" :label="t('webhooks.editor.allowPrivate')" />
        <div class="hook-editor__hint">{{ t('webhooks.editor.allowPrivateHint') }}</div>
      </section>
    </div>
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { Webhook, WebhookEventName, WebhookWrite } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { useCopy, useNotify } from '@/composables/useNotify'
import { type MessageKey, t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'
import { iconAdd, iconCopy, iconDelete, iconHide, iconRefresh, iconShow } from '@/utils/icons'

import { ALL_EVENTS, compactEvents, EVENT_GROUPS, eventLabel, expandEvents, generateSecret, type HeaderRow, headerProblem, rowsToHeaders, SECRET_MIN, urlProblem } from './webhooks'

const props = defineProps<{ webhook: Webhook | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [hook: Webhook] }>()
const notify = useNotify()
const copy = useCopy()
const repos = useRepositoriesStore()

const name = ref('')
const description = ref('')
const url = ref('')
const enabled = ref(true)
const everything = ref(true)
const selected = ref<WebhookEventName[]>([])
const repositories = ref<string[]>([])
const secret = ref('')
const showSecret = ref(false)
const replaceHeaders = ref(false)
const headerRows = ref<HeaderRow[]>([])
const timeoutS = ref(10)
const maxAttempts = ref(8)
const allowPrivate = ref(false)
const saving = ref(false)
const triedSubmit = ref(false)
const formError = ref<string | null>(null)
const errors = ref<Record<string, string>>({})
const repoOptions = ref<string[]>([])

watch(open, (isOpen) => {
  if (!isOpen) return
  const w = props.webhook
  name.value = w?.name ?? ''
  description.value = w?.description ?? ''
  url.value = w?.url ?? ''
  enabled.value = w?.enabled ?? true
  everything.value = !w || w.events.includes('*')
  selected.value = w ? [...expandEvents(w.events)] : ['content.upload']
  repositories.value = [...(w?.repositories ?? [])]
  secret.value = w ? '' : generateSecret()
  showSecret.value = !w
  replaceHeaders.value = !w
  headerRows.value = []
  timeoutS.value = w?.timeout_s ?? 10
  maxAttempts.value = w?.max_attempts ?? 8
  allowPrivate.value = w?.allow_private ?? false
  errors.value = {}
  formError.value = null
  triedSubmit.value = false
  void repos.load().catch(() => undefined)
})

const eventsError = computed(() => (triedSubmit.value && !everything.value && !selected.value.length ? t('webhooks.editor.eventsRequired') : (errors.value.events ?? null)))

function secretRule(value: string): true | string {
  if (!value) return props.webhook ? true : t('webhooks.editor.secretRequired')
  return value.length >= SECRET_MIN || t('webhooks.editor.secretShort', { min: SECRET_MIN })
}

function rowProblem(row: HeaderRow): string | null {
  if (!row.name.trim() && !row.value) return null
  return headerProblem(row.name, row.value)
}

function generate(): void {
  secret.value = generateSecret()
  showSecret.value = true
}

function startReplace(): void {
  replaceHeaders.value = true
  headerRows.value = (props.webhook?.header_names ?? []).map((n) => ({ name: n, value: '' }))
  if (!headerRows.value.length) headerRows.value.push({ name: '', value: '' })
}

function filterRepos(input: string, update: (fn: () => void) => void): void {
  update(() => {
    const needle = input.toLowerCase()
    repoOptions.value = repos.items.map((r) => r.name).filter((n) => n.toLowerCase().includes(needle)).slice(0, 50)
  })
}

async function save(): Promise<void> {
  triedSubmit.value = true
  if (!everything.value && !selected.value.length) return
  if (replaceHeaders.value && headerRows.value.some((r) => rowProblem(r))) return
  saving.value = true
  formError.value = null
  errors.value = {}
  const body: WebhookWrite = {
    name: name.value.trim(),
    description: description.value.trim(),
    url: url.value.trim(),
    events: everything.value ? ['*'] : compactEvents(selected.value.filter((e) => ALL_EVENTS.includes(e))),
    repositories: repositories.value.map((r) => r.trim()).filter(Boolean),
    enabled: enabled.value,
    secret: secret.value || null,
    headers: replaceHeaders.value ? rowsToHeaders(headerRows.value) : null,
    allow_private: allowPrivate.value,
    timeout_s: timeoutS.value,
    max_attempts: maxAttempts.value,
  }
  try {
    const saved = props.webhook
      ? await call(api.PUT('/webhooks/{webhook_id}', { params: { path: { webhook_id: props.webhook.id } }, body }))
      : await call(api.POST('/webhooks', { body }))
    notify.success(props.webhook ? t('webhooks.editor.saved', { name: saved.name }) : t('webhooks.editor.created', { name: saved.name }))
    open.value = false
    emit('saved', saved)
  } catch (error) {
    if (error instanceof ApiError && error.problem.errors?.length) errors.value = error.fieldErrors
    else formError.value = errorMessage(error)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.hook-editor {
  display: grid;
  gap: 12px;
}

.hook-editor__row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 220px;
  gap: 12px;
  align-items: start;
}

.hook-editor__row--even {
  grid-template-columns: 1fr 1fr;
}

.hook-editor__enabled {
  padding-top: 2px;
}

.hook-editor__section {
  display: grid;
  gap: 8px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}

.hook-editor__title {
  font-size: var(--fs-14);
  font-weight: 650;
}

.hook-editor__hint {
  margin-top: -4px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.hook-editor__events {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.hook-editor__group {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 8px 12px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.hook-editor__group legend {
  padding: 0 4px;
  font-size: var(--fs-12);
  font-weight: 600;
  color: var(--text-secondary);
}

.hook-editor__event :deep(.q-checkbox__label) {
  display: grid;
  gap: 2px;
}

.hook-editor__event-help {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.hook-editor__need {
  font-size: var(--fs-13);
  color: var(--status-blocked);
}

.hook-editor__verify {
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.hook-editor__verify summary {
  display: list-item;
  cursor: pointer;
  color: var(--text-primary);
}

.hook-editor__verify p {
  margin: 6px 0 0;
}

.hook-editor__stored {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: var(--fs-13);
}

.hook-editor__header {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 40px;
  gap: 8px;
  align-items: start;
}

.hook-editor__header-actions {
  display: flex;
  gap: 8px;
}

.hook-editor__error {
  margin-bottom: 14px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--cache-error-bg);
  color: var(--cache-error);
  font-size: var(--fs-13);
}

@media (max-width: 599px) {
  .hook-editor__row,
  .hook-editor__row--even,
  .hook-editor__events {
    grid-template-columns: 1fr;
  }
}
</style>
