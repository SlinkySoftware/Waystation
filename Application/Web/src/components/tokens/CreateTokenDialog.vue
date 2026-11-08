<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Create an API token: name, scopes (only those the owner holds are selectable), optional repository limits,
     expiry. Emits the created token with its secret, which the caller shows once (TokenSecretDialog). -->
<template>
  <FormDialog v-model="open" :title="t('tokens.form.title')" :caption="t('tokens.form.caption')" :saving="saving" :submit-label="t('tokens.form.submit')" width="600px" @submit="save">
    <div v-if="formError" class="form-error" role="alert">{{ formError }}</div>
    <div class="token-form">
      <q-input
        v-model="name"
        outlined
        dense
        autofocus
        maxlength="64"
        :label="t('tokens.form.name')"
        :hint="t('tokens.form.nameHint')"
        :rules="[(v: string) => Boolean(v?.trim()) || t('tokens.form.nameRequired')]"
        :error="Boolean(fieldErrors.name)"
        :error-message="fieldErrors.name"
      />

      <fieldset class="token-form__group">
        <legend class="token-form__legend">{{ t('tokens.form.scopes') }}</legend>
        <div class="token-form__scopes">
          <label v-for="scope in SCOPES" :key="scope" class="scope" :class="{ 'scope--off': !available.includes(scope), 'scope--on': scopes.includes(scope) }">
            <q-checkbox v-model="scopes" :val="scope" dense :disable="!available.includes(scope)" :aria-describedby="`scope-help-${scope}`" />
            <span class="scope__text">
              <span class="scope__name">{{ scopeLabel(scope) }}</span>
              <span :id="`scope-help-${scope}`" class="scope__help">{{ available.includes(scope) ? scopeHelp(scope) : t('tokens.form.notHeld') }}</span>
            </span>
          </label>
        </div>
        <div v-if="scopeError" class="token-form__error" role="alert">{{ scopeError }}</div>
      </fieldset>

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
        :label="t('tokens.form.repositories')"
        :hint="t('tokens.form.repositoriesHint')"
        :error="Boolean(repoError)"
        :error-message="repoError ?? undefined"
        @filter="filterRepos"
      />

      <div class="token-form__expiry">
        <q-select v-model="expiry" outlined dense emit-value map-options :options="EXPIRY_CHOICES" :label="t('tokens.form.expiry')" class="token-form__expiry-select" />
        <q-input
          v-if="expiry === 'custom'"
          v-model="customDate"
          outlined
          dense
          readonly
          :label="t('tokens.form.expiresOn')"
          :rules="[(v: string) => (Boolean(v) && isFutureDate(v)) || t('tokens.form.dateRequired')]"
          :error="Boolean(fieldErrors.expires_at)"
          :error-message="fieldErrors.expires_at"
          class="token-form__date"
        >
          <q-popup-proxy cover transition-show="scale" transition-hide="scale">
            <q-date v-model="customDate" :options="(d: string) => isFutureDate(d)" minimal />
          </q-popup-proxy>
        </q-input>
        <div v-else-if="expiry !== 'never'" class="token-form__expires-on">
          {{ t('tokens.form.expiresOn') }} {{ absoluteDate(new Date(expiresAt(expiry, null) ?? Date.now())) }}
        </div>
      </div>
      <div v-if="expiry === 'never'" class="token-form__warning" role="note">
        <q-icon :name="iconWarning" size="18px" />
        {{ t('tokens.form.neverWarning') }}
      </div>
    </div>
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { ApiTokenCreated, TokenScope } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'
import { absoluteDate } from '@/utils/format'
import { iconWarning } from '@/utils/icons'

import { dateModel, EXPIRY_CHOICES, type ExpiryChoice, expiresAt, isFutureDate, SCOPES, scopeHelp, scopeLabel } from './tokens'

const props = defineProps<{ available: TokenScope[] }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ created: [created: ApiTokenCreated] }>()

const repos = useRepositoriesStore()
const name = ref('')
const scopes = ref<TokenScope[]>([])
const repositories = ref<string[]>([])
const expiry = ref<ExpiryChoice>('90')
const customDate = ref(dateModel(30))
const saving = ref(false)
const formError = ref<string | null>(null)
const fieldErrors = ref<Record<string, string>>({})
const triedSubmit = ref(false)
const repoOptions = ref<string[]>([])

watch(open, (isOpen) => {
  if (!isOpen) return
  name.value = ''
  scopes.value = props.available.includes('read') ? ['read'] : []
  repositories.value = []
  expiry.value = '90'
  customDate.value = dateModel(30)
  formError.value = null
  fieldErrors.value = {}
  triedSubmit.value = false
  void repos.load().catch(() => undefined)
})

const scopeError = computed(() => {
  const server = Object.entries(fieldErrors.value).find(([field]) => field.startsWith('scopes'))?.[1]
  if (server) return server
  return triedSubmit.value && scopes.value.length === 0 ? t('tokens.form.scopesRequired') : null
})
const repoError = computed(() => Object.entries(fieldErrors.value).find(([field]) => field.startsWith('repositories'))?.[1] ?? null)

function filterRepos(value: string, update: (fn: () => void) => void): void {
  update(() => {
    const needle = value.toLowerCase()
    repoOptions.value = repos.items.map((r) => r.name).filter((n) => n.toLowerCase().includes(needle) && !repositories.value.includes(n))
  })
}

async function save(): Promise<void> {
  triedSubmit.value = true
  if (scopes.value.length === 0) return
  saving.value = true
  formError.value = null
  fieldErrors.value = {}
  try {
    const created = await call(
      api.POST('/auth/me/tokens', {
        body: {
          name: name.value.trim(),
          scopes: SCOPES.filter((s) => scopes.value.includes(s)),
          repositories: repositories.value,
          expires_at: expiresAt(expiry.value, customDate.value),
        },
      }),
    )
    open.value = false
    emit('created', created)
  } catch (error) {
    if (error instanceof ApiError && error.status === 422) {
      fieldErrors.value = error.fieldErrors
      if (!Object.keys(fieldErrors.value).length) formError.value = errorMessage(error)
    } else {
      formError.value = errorMessage(error)
    }
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.token-form {
  display: grid;
  gap: 16px;
}

.token-form__group {
  border: 0;
  margin: 0;
  padding: 0;
  min-width: 0;
}

.token-form__legend {
  font-size: var(--fs-13);
  font-weight: 600;
  margin-bottom: 8px;
  padding: 0;
}

.token-form__scopes {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 8px;
}

.scope {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: border-color 150ms ease-out, background 150ms ease-out;
}

.scope--on {
  border-color: var(--q-primary);
  background: var(--surface-brand);
}

.scope--off {
  cursor: not-allowed;
  opacity: 0.6;
}

.scope__text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.scope__name {
  font-weight: 600;
}

.scope__help {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.token-form__error {
  margin-top: 6px;
  font-size: var(--fs-12);
  color: var(--status-blocked);
}

.token-form__expiry {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 12px;
}

.token-form__expiry-select {
  min-width: 180px;
}

.token-form__date {
  min-width: 180px;
}

.token-form__expires-on {
  align-self: center;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.form-error {
  margin-bottom: 14px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--cache-error-bg);
  color: var(--cache-error);
  font-size: var(--fs-13);
}

.token-form__warning {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  color: var(--status-degraded);
  background: var(--status-degraded-bg);
  font-size: var(--fs-13);
}
</style>
