<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Sectioned proxy repository form (create and settings). Field errors come from problem+json `errors[]`. -->
<template>
  <div class="repo-form">
    <div v-if="mode === 'create' && presets.length" class="repo-form__presets">
      <span class="text-caption">{{ t('repoForm.presets') }}</span>
      <q-chip v-for="p in presets" :key="p.id" clickable outline :label="p.label" class="repo-form__preset" @click="applyPreset(p)" />
    </div>

    <section class="repo-form__section">
      <h3 class="section-label">{{ t('repoForm.general') }}</h3>
      <q-input
        v-model="model.name"
        outlined
        dense
        :label="t('repoForm.name')"
        :readonly="mode === 'edit'"
        :hint="mode === 'create' ? t('repoForm.nameHint', { url: `${baseUrl}/repository/${model.name || '<name>'}/` }) : undefined"
        :rules="mode === 'create' ? [(v: string) => NAME_PATTERN.test(v) || t('repoForm.nameInvalid')] : []"
        :error="!!errors.name"
        :error-message="errors.name"
        autocomplete="off"
        spellcheck="false"
      />
      <q-input v-model="model.description" outlined dense :label="t('repoForm.description')" maxlength="500" />
      <q-toggle v-model="model.online" :label="t('repoForm.online')" />
    </section>

    <section class="repo-form__section">
      <h3 class="section-label">{{ t('repoForm.upstream') }}</h3>
      <q-input
        v-model="model.upstreamUrl"
        outlined
        dense
        type="url"
        :label="t('repoForm.upstreamUrl')"
        :rules="[(v: string) => /^https?:\/\/.+/.test(v.trim()) || t('repoForm.urlInvalid')]"
        :error="!!errors['proxy.upstream_url']"
        :error-message="errors['proxy.upstream_url']"
        spellcheck="false"
        class="mono-input"
      />
      <UpstreamTest :model="model" :repository="mode === 'edit' ? model.name : null" />
      <div class="repo-form__row">
        <q-select v-model="model.authKind" outlined dense emit-value map-options :options="authOptions" :label="t('repoForm.auth')" class="col" />
        <q-input v-if="model.authKind === 'basic'" v-model="model.username" outlined dense :label="t('repoForm.username')" class="col" :error="!!errors['proxy.auth.username']" :error-message="errors['proxy.auth.username']" autocomplete="off" />
      </div>
      <q-input
        v-if="model.authKind !== 'none'"
        v-model="model.secret"
        outlined
        dense
        type="password"
        autocomplete="new-password"
        :label="model.authKind === 'bearer' ? t('repoForm.token') : t('repoForm.password')"
        :hint="model.secretSet ? t('repoForm.secretKept') : t('repoForm.secretHint')"
        :error="!!errors['proxy.auth.secret']"
        :error-message="errors['proxy.auth.secret']"
      />
    </section>

    <section v-if="model.format !== 'yum'" class="repo-form__section">
      <h3 class="section-label">{{ t(`repoForm.options.${model.format}`) }}</h3>
      <template v-if="model.format === 'docker'">
        <q-select
          v-model="model.aliases"
          outlined
          dense
          multiple
          use-chips
          use-input
          new-value-mode="add-unique"
          hide-dropdown-icon
          :label="t('repoForm.aliases')"
          :hint="t('repoForm.aliasesHint')"
          :error="!!errors['format_options.aliases']"
          :error-message="errors['format_options.aliases']"
        />
        <q-toggle v-model="model.dockerHub" :label="t('repoForm.dockerHub')" />
        <q-toggle v-model="model.defaultRepo" :label="t('repoForm.defaultRepo')" />
      </template>
      <template v-else-if="model.format === 'npm'">
        <q-toggle v-model="model.auditPassthrough" :label="t('repoForm.audit')" />
      </template>
      <template v-else-if="model.format === 'apt'">
        <div class="repo-form__row">
          <q-input
            v-model="model.distribution"
            outlined
            dense
            :label="t('repoForm.distribution')"
            :hint="t('repoForm.distributionHint')"
            class="col"
            :error="!!errors['format_options.distribution']"
            :error-message="errors['format_options.distribution']"
          />
          <q-toggle v-model="model.flat" :label="t('repoForm.flat')" class="col-auto" />
        </div>
      </template>
      <template v-else>
        <q-input v-model="model.metadataRegex" outlined dense :label="t('repoForm.metadataRegex')" :hint="t('repoForm.metadataRegexHint')" class="mono-input" />
        <q-select v-model="model.contentDisposition" outlined dense emit-value map-options :options="dispositionOptions" :label="t('repoForm.disposition')" />
      </template>
    </section>

    <q-expansion-item dense dense-toggle switch-toggle-side :label="t('repoForm.caching')" class="repo-form__advanced" header-class="section-label">
      <div class="repo-form__section repo-form__section--inner">
        <div class="repo-form__row">
          <q-input v-model.number="model.metadataMaxAgeMin" outlined dense type="number" min="0" :label="t('repoForm.metadataMaxAge')" :suffix="t('repoForm.minutes')" class="col" />
          <q-input v-model.number="model.negativeTtlMin" outlined dense type="number" min="0" :label="t('repoForm.negativeTtl')" :suffix="t('repoForm.minutes')" class="col" />
        </div>
        <q-toggle v-model="model.staleOnError" :label="t('repoForm.staleOnError')" />
        <q-toggle v-model="model.offline" :label="t('repoForm.offline')" />
        <q-toggle v-model="model.allowPrivate" :label="t('repoForm.allowPrivate')" />
      </div>
    </q-expansion-item>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { t } from '@/i18n'
import { useConfigStore } from '@/stores/config'

import { NAME_PATTERN, type Preset, PRESETS, type RepoFormModel } from './form'
import UpstreamTest from './UpstreamTest.vue'

const props = withDefaults(defineProps<{ mode?: 'create' | 'edit'; errors?: Record<string, string> }>(), { mode: 'create', errors: () => ({}) })
const model = defineModel<RepoFormModel>({ required: true })

const config = useConfigStore()
const baseUrl = computed(() => config.config?.base_url ?? '')
const presets = computed(() => (props.mode === 'create' ? (model.value.format === 'yum' ? [] : PRESETS[model.value.format]) : []))

const authOptions = computed(() => [
  { label: t('repoForm.authNone'), value: 'none' },
  { label: t('repoForm.authBasic'), value: 'basic' },
  { label: t('repoForm.authBearer'), value: 'bearer' },
])
const dispositionOptions = computed(() => [
  { label: t('repoForm.attachment'), value: 'attachment' },
  { label: t('repoForm.inline'), value: 'inline' },
])

function applyPreset(p: Preset): void {
  model.value = { ...model.value, ...p.apply }
}
</script>

<style scoped>
.repo-form {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.repo-form__presets {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 6px;
}

.repo-form__preset {
  margin: 0;
}

.repo-form__section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.repo-form__section--inner {
  padding: 10px 0 4px;
}

.repo-form__section h3 {
  margin: 0;
}

.repo-form__row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.repo-form__row > .col {
  min-width: 180px;
}

.repo-form__advanced :deep(.q-item) {
  padding-left: 0;
}

.mono-input :deep(input) {
  font-family: var(--font-mono);
  font-size: var(--fs-13);
}
</style>
