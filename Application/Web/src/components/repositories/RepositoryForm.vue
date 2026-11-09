<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Sectioned repository form (create and settings; proxy and hosted). Field errors come from problem+json `errors[]`. -->
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

    <section v-if="model.type === 'hosted'" class="repo-form__section">
      <h3 class="section-label">{{ t('repoForm.uploads') }}</h3>
      <q-select
        v-model="model.writePolicy"
        outlined
        dense
        emit-value
        map-options
        :options="policyOptions"
        :label="t('repoForm.writePolicy')"
        :hint="t('repoForm.writePolicyHint')"
        :error="!!errors['hosted.write_policy']"
        :error-message="errors['hosted.write_policy']"
      />
      <q-select
        v-if="model.format === 'docker'"
        v-model="model.immutableTags"
        outlined
        dense
        multiple
        use-chips
        use-input
        new-value-mode="add-unique"
        hide-dropdown-icon
        :label="t('repoForm.immutableTags')"
        :hint="t('repoForm.immutableTagsHint')"
        :error="!!immutableTagsError"
        :error-message="immutableTagsError"
        class="mono-input"
      />
    </section>

    <section v-if="model.type === 'group'" class="repo-form__section">
      <h3 class="section-label">{{ t('repoForm.members') }}</h3>
      <q-select
        v-model="model.members"
        outlined
        dense
        multiple
        use-chips
        emit-value
        map-options
        :options="memberOptions"
        :label="t('repoForm.membersLabel')"
        :hint="t('repoForm.membersHint')"
        :error="!!membersError"
        :error-message="membersError"
      />
      <q-select
        v-if="model.format === 'docker'"
        v-model="model.writableMember"
        outlined
        dense
        emit-value
        map-options
        clearable
        :options="writableOptions"
        :label="t('repoForm.writableMember')"
        :hint="t('repoForm.writableMemberHint')"
        :error="!!errors['group.writable_member']"
        :error-message="errors['group.writable_member']"
      />
    </section>

    <section v-if="model.type === 'proxy'" class="repo-form__section">
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

    <section v-if="model.format !== 'yum' && !(model.format === 'maven' && model.type === 'group')" class="repo-form__section">
      <h3 class="section-label">{{ t(`repoForm.options.${model.format}`) }}</h3>
      <template v-if="model.format === 'docker' && model.type !== 'proxy'">
        <q-toggle v-model="model.defaultRepo" :label="t('repoForm.defaultRepoHosted')" />
      </template>
      <template v-else-if="model.format === 'docker'">
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
      <template v-else-if="model.format === 'maven'">
        <div class="repo-form__row">
          <q-select v-model="model.versionPolicy" outlined dense emit-value map-options :options="versionPolicyOptions" :label="t('repoForm.versionPolicy')" :hint="t('repoForm.versionPolicyHint')" class="col" />
          <q-select v-model="model.layoutPolicy" outlined dense emit-value map-options :options="layoutPolicyOptions" :label="t('repoForm.layoutPolicy')" :hint="t('repoForm.layoutPolicyHint')" class="col" />
        </div>
      </template>
      <template v-else>
        <q-input v-model="model.metadataRegex" outlined dense :label="t('repoForm.metadataRegex')" :hint="t('repoForm.metadataRegexHint')" class="mono-input" />
        <q-select v-model="model.contentDisposition" outlined dense emit-value map-options :options="dispositionOptions" :label="t('repoForm.disposition')" />
      </template>
    </section>

    <q-expansion-item v-if="model.type === 'proxy'" v-model="cachingOpen" dense dense-toggle switch-toggle-side :label="t('repoForm.caching')" class="repo-form__advanced" header-class="section-label">
      <div class="repo-form__section repo-form__section--inner">
        <div class="repo-form__row">
          <q-input v-model.number="model.metadataMaxAgeMin" outlined dense type="number" min="0" :label="t('repoForm.metadataMaxAge')" :suffix="t('repoForm.minutes')" class="col" />
          <q-input v-model.number="model.negativeTtlMin" outlined dense type="number" min="0" :label="t('repoForm.negativeTtl')" :suffix="t('repoForm.minutes')" class="col" />
        </div>
        <q-toggle v-model="model.staleOnError" :label="t('repoForm.staleOnError')" />
        <q-toggle v-model="model.offline" :label="t('repoForm.offline')" />
        <q-toggle v-model="model.allowPrivate" :label="t('repoForm.allowPrivate')" />
        <q-toggle v-model="model.autoBlock" :label="t('repoForm.autoBlock')" />
        <div v-if="model.autoBlock" class="repo-form__row">
          <q-input
            v-model.number="model.autoBlockFailures"
            outlined
            dense
            type="number"
            min="1"
            max="1000"
            :label="t('repoForm.autoBlockFailures')"
            class="col"
            :error="!!errors['proxy.auto_block.failures']"
            :error-message="errors['proxy.auto_block.failures']"
          />
          <q-input
            v-model.number="model.autoBlockInitialS"
            outlined
            dense
            type="number"
            min="1"
            :label="t('repoForm.autoBlockInitial')"
            :suffix="t('repoForm.seconds')"
            class="col"
            :error="!!errors['proxy.auto_block.initial_s']"
            :error-message="errors['proxy.auto_block.initial_s']"
          />
          <q-input
            v-model.number="model.autoBlockMaxMin"
            outlined
            dense
            type="number"
            min="1"
            :label="t('repoForm.autoBlockMax')"
            :suffix="t('repoForm.minutes')"
            class="col"
            :error="!!errors['proxy.auto_block.max_s']"
            :error-message="errors['proxy.auto_block.max_s']"
          />
        </div>
        <div v-if="model.autoBlock" class="text-caption text-muted">{{ t('repoForm.autoBlockHint') }}</div>
      </div>
    </q-expansion-item>

    <q-expansion-item
      v-if="model.type !== 'group'"
      v-model="quotaOpen"
      dense
      dense-toggle
      switch-toggle-side
      :label="t('repoForm.quota')"
      class="repo-form__advanced"
      header-class="section-label"
    >
      <div class="repo-form__section repo-form__section--inner">
        <q-toggle v-model="model.quotaEnabled" :label="t('repoForm.quotaEnabled')" />
        <template v-if="model.quotaEnabled">
          <div class="repo-form__row">
            <q-input
              v-model.number="model.quotaSize"
              outlined
              dense
              type="number"
              min="0"
              step="any"
              :label="t('repoForm.quotaSize')"
              class="col"
              :rules="[(v: number) => (Number.isFinite(v) && v > 0) || t('repoForm.quotaSizeInvalid')]"
              :error="!!errors['storage.quota_bytes']"
              :error-message="errors['storage.quota_bytes']"
            />
            <q-select v-model="model.quotaUnit" outlined dense :options="[...QUOTA_UNITS]" :label="t('repoForm.quotaUnit')" class="repo-form__unit" />
          </div>
          <q-select
            v-model="model.quotaMode"
            outlined
            dense
            emit-value
            map-options
            :options="quotaModeOptions"
            :label="t('repoForm.quotaMode')"
            :hint="model.quotaMode === 'hard' ? t('repoForm.quotaHintHard') : t('repoForm.quotaHintSoft')"
            :error="!!errors['storage.quota_mode']"
            :error-message="errors['storage.quota_mode']"
          />
          <div class="text-caption text-muted">{{ t('repoForm.quotaShared') }}</div>
        </template>
      </div>
    </q-expansion-item>

    <q-expansion-item v-model="networkOpen" dense dense-toggle switch-toggle-side :label="t('repoForm.network')" class="repo-form__advanced" header-class="section-label">
      <div class="repo-form__section repo-form__section--inner">
        <q-input
          v-model="model.httpProxy"
          outlined
          dense
          :label="t('repoForm.httpProxy')"
          :hint="t('repoForm.httpProxyHint')"
          spellcheck="false"
          autocomplete="off"
          class="mono-input"
          :error="!!errors['proxy.http_proxy']"
          :error-message="errors['proxy.http_proxy']"
        />
        <q-input
          v-model="model.caPem"
          outlined
          dense
          autogrow
          type="textarea"
          :label="t('repoForm.caPem')"
          :hint="t('repoForm.caPemHint')"
          spellcheck="false"
          class="mono-input repo-form__pem"
          :error="!!errors['proxy.ca_pem']"
          :error-message="errors['proxy.ca_pem']"
        />
      </div>
    </q-expansion-item>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { t } from '@/i18n'
import { useConfigStore } from '@/stores/config'
import { useRepositoriesStore } from '@/stores/repositories'
import { typeMeta } from '@/utils/domain'
import { QUOTA_UNITS } from '@/utils/quota'

import { NAME_PATTERN, type Preset, PRESETS, type RepoFormModel } from './form'
import UpstreamTest from './UpstreamTest.vue'

const props = withDefaults(defineProps<{ mode?: 'create' | 'edit'; errors?: Record<string, string> }>(), { mode: 'create', errors: () => ({}) })
const model = defineModel<RepoFormModel>({ required: true })

const config = useConfigStore()
const baseUrl = computed(() => config.config?.base_url ?? '')
const presets = computed(() => (props.mode === 'create' && model.value.type === 'proxy' ? (model.value.format === 'yum' ? [] : PRESETS[model.value.format]) : []))

const repositories = useRepositoriesStore()
void repositories.load().catch(() => undefined)
/** Group members: repositories of the same format that are not groups (and not this one), in name order. */
const memberOptions = computed(() =>
  repositories.items
    .filter((r) => r.format === model.value.format && r.type !== 'group' && r.name !== model.value.name)
    .map((r) => ({ label: `${r.name} (${typeMeta(r.type).label})`, value: r.name })),
)
const writableOptions = computed(() =>
  model.value.members
    .filter((name) => repositories.byName.get(name)?.type === 'hosted')
    .map((name) => ({ label: name, value: name })),
)
const membersError = computed(() => Object.entries(props.errors).find(([field]) => field.startsWith('group.members') || field === 'group')?.[1] ?? '')

const policyOptions = computed(() =>
  (['allow_redeploy', 'disable_redeploy', 'read_only'] as const).map((value) => ({ label: t(`repoForm.policy.${value}`), value })),
)
/** The server reports pattern errors per item (hosted.immutable_tag_patterns.<i>). */
const immutableTagsError = computed(() => Object.entries(props.errors).find(([field]) => field.startsWith('hosted.immutable_tag_patterns'))?.[1] ?? '')

const authOptions = computed(() => [
  { label: t('repoForm.authNone'), value: 'none' },
  { label: t('repoForm.authBasic'), value: 'basic' },
  { label: t('repoForm.authBearer'), value: 'bearer' },
])
const versionPolicyOptions = computed(() => [
  { label: t('repoForm.versionPolicies.release'), value: 'release' },
  { label: t('repoForm.versionPolicies.snapshot'), value: 'snapshot' },
  { label: t('repoForm.versionPolicies.mixed'), value: 'mixed' },
])
const layoutPolicyOptions = computed(() => [
  { label: t('repoForm.layoutPolicies.strict'), value: 'strict' },
  { label: t('repoForm.layoutPolicies.permissive'), value: 'permissive' },
])
const dispositionOptions = computed(() => [
  { label: t('repoForm.attachment'), value: 'attachment' },
  { label: t('repoForm.inline'), value: 'inline' },
])

// Advanced sections open by themselves when they hold a setting or a field error.
const cachingOpen = ref(false)
const networkOpen = ref(Boolean(model.value.caPem || model.value.httpProxy))
const quotaOpen = ref(model.value.quotaEnabled)
const quotaModeOptions = computed(() => [
  { label: t('repoForm.quotaModeSoft'), value: 'soft' },
  { label: t('repoForm.quotaModeHard'), value: 'hard' },
])
watch(
  () => props.errors,
  (errors) => {
    const fields = Object.keys(errors)
    if (fields.some((f) => f === 'proxy.ca_pem' || f === 'proxy.http_proxy')) networkOpen.value = true
    if (fields.some((f) => f.startsWith('storage.'))) quotaOpen.value = true
    if (fields.some((f) => f.startsWith('proxy.auto_block.') || f.endsWith('_age_s') || f === 'proxy.negative_ttl_s')) cachingOpen.value = true
  },
  { immediate: true },
)

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

.repo-form__unit {
  width: 110px;
}

.repo-form__advanced :deep(.q-item) {
  padding-left: 0;
}

.repo-form__pem :deep(textarea) {
  font-family: var(--font-mono);
  font-size: var(--fs-12);
  max-height: 240px;
}

.mono-input :deep(input) {
  font-family: var(--font-mono);
  font-size: var(--fs-13);
}
</style>
