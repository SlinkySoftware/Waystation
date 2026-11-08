<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- OpenID Connect provider settings: issuer + client, redirect URIs to register, claims, sign-in behaviour, token checks. -->
<template>
  <section class="ldap-section">
    <h3 class="section-label">{{ t('oidcForm.provider') }}</h3>
    <q-input
      v-model="oidc.issuer"
      outlined
      dense
      class="mono-input"
      :label="t('oidcForm.issuer')"
      :hint="t('oidcForm.issuerHint')"
      :placeholder="t('oidcForm.issuerPlaceholder')"
      :rules="[(v: string) => !!v.trim() || t('ldapForm.required')]"
      :error="!!errors['oidc.issuer']"
      :error-message="errors['oidc.issuer']"
      spellcheck="false"
      autocomplete="off"
    />
    <div v-if="insecure" class="ldap-section__warning" role="alert">
      <q-icon :name="iconWarning" size="18px" />
      {{ t('oidcForm.httpWarning') }}
    </div>
    <div class="ldap-section__row">
      <q-input
        v-model="oidc.client_id"
        outlined
        dense
        class="col mono-input"
        :label="t('oidcForm.clientId')"
        :rules="[(v: string) => !!v.trim() || t('ldapForm.required')]"
        :error="!!errors['oidc.client_id']"
        :error-message="errors['oidc.client_id']"
        spellcheck="false"
        autocomplete="off"
      />
      <q-select
        v-model="oidc.client_auth"
        outlined
        dense
        emit-value
        map-options
        class="col"
        :options="authOptions"
        :label="t('oidcForm.clientAuth')"
        :error="!!errors['oidc.client_auth']"
        :error-message="errors['oidc.client_auth']"
      />
    </div>
    <q-input
      v-if="oidc.client_auth !== 'none'"
      v-model="secret"
      outlined
      dense
      type="password"
      autocomplete="new-password"
      :label="t('oidcForm.clientSecret')"
      :hint="model.clientSecretSet ? t('oidcForm.clientSecretKept') : t('oidcForm.clientSecretHint')"
      :error="!!errors['oidc.client_secret']"
      :error-message="errors['oidc.client_secret']"
    />

    <div class="oidc-uris">
      <div class="ldap-section__label">{{ t('oidcForm.registration') }}</div>
      <template v-if="model.redirectUri">
        <div class="oidc-uris__row">
          <span class="oidc-uris__name">{{ t('oidcForm.redirectUri') }}</span>
          <CopyField :value="model.redirectUri" :label="t('oidcForm.redirectUri')" compact />
        </div>
        <div v-if="model.postLogoutRedirectUri" class="oidc-uris__row">
          <span class="oidc-uris__name">{{ t('oidcForm.postLogoutUri') }}</span>
          <CopyField :value="model.postLogoutRedirectUri" :label="t('oidcForm.postLogoutUri')" compact />
        </div>
      </template>
      <div v-else class="text-caption text-muted">{{ t('oidcForm.afterSave') }}</div>
    </div>

    <q-select
      v-model="oidc.scopes"
      outlined
      dense
      multiple
      use-chips
      use-input
      hide-dropdown-icon
      new-value-mode="add-unique"
      input-debounce="0"
      :label="t('oidcForm.scopes')"
      :hint="t('oidcForm.scopesHint')"
      :error="!!errors['oidc.scopes']"
      :error-message="errors['oidc.scopes']"
    />
  </section>

  <section class="ldap-section">
    <h3 class="section-label">{{ t('oidcForm.claims') }}</h3>
    <div class="ldap-section__row">
      <q-input v-model="oidc.username_claim" outlined dense class="col mono-input" :label="t('oidcForm.usernameClaim')" :hint="t('oidcForm.usernameClaimHint')" :error="!!errors['oidc.username_claim']" :error-message="errors['oidc.username_claim']" spellcheck="false" />
      <q-input v-model="oidc.display_name_claim" outlined dense class="col mono-input" :label="t('oidcForm.displayNameClaim')" :error="!!errors['oidc.display_name_claim']" :error-message="errors['oidc.display_name_claim']" spellcheck="false" />
      <q-input v-model="oidc.email_claim" outlined dense class="col mono-input" :label="t('oidcForm.emailClaim')" :error="!!errors['oidc.email_claim']" :error-message="errors['oidc.email_claim']" spellcheck="false" />
    </div>
    <div class="ldap-section__row">
      <q-input v-model="groupsClaim" outlined dense class="col mono-input" :label="t('oidcForm.groupsClaim')" :hint="t('oidcForm.groupsClaimHint')" :error="!!errors['oidc.groups_claim']" :error-message="errors['oidc.groups_claim']" spellcheck="false" />
      <q-input v-model="requireGroup" outlined dense class="col" :label="t('oidcForm.requireGroup')" :hint="t('oidcForm.requireGroupHint')" :error="!!errors['oidc.require_group']" :error-message="errors['oidc.require_group']" spellcheck="false" />
    </div>
  </section>

  <section class="ldap-section">
    <h3 class="section-label">{{ t('oidcForm.behaviour') }}</h3>
    <div class="oidc-toggles">
      <q-toggle v-model="oidc.auto_provision" :label="t('oidcForm.autoProvision')" />
      <div class="text-caption text-muted oidc-toggles__hint">{{ t('oidcForm.autoProvisionHint') }}</div>
      <q-toggle v-model="oidc.use_userinfo" :label="t('oidcForm.useUserinfo')" />
      <div class="text-caption text-muted oidc-toggles__hint">{{ t('oidcForm.useUserinfoHint') }}</div>
      <q-toggle v-model="oidc.rp_logout" :label="t('oidcForm.rpLogout')" />
      <div class="text-caption text-muted oidc-toggles__hint">{{ t('oidcForm.rpLogoutHint') }}</div>
      <q-toggle v-model="oidc.allow_private" :label="t('oidcForm.allowPrivate')" />
      <div class="text-caption text-muted oidc-toggles__hint">{{ t('oidcForm.allowPrivateHint') }}</div>
    </div>
  </section>

  <section class="ldap-section">
    <h3 class="section-label">{{ t('oidcForm.tokenChecks') }}</h3>
    <div class="ldap-section__row">
      <q-select
        v-model="oidc.signing_algs"
        outlined
        dense
        multiple
        use-chips
        class="col"
        :options="SIGNING_ALGS"
        :label="t('oidcForm.signingAlgs')"
        :hint="t('oidcForm.signingAlgsHint')"
        :rules="[(v: string[]) => v.length > 0 || t('ldapForm.required')]"
        :error="!!errors['oidc.signing_algs']"
        :error-message="errors['oidc.signing_algs']"
      />
      <q-input v-model.number="oidc.timeout_s" outlined dense type="number" min="1" max="60" class="oidc-timeout" :label="t('oidcForm.timeout')" :suffix="t('ldapForm.seconds')" :error="!!errors['oidc.timeout_s']" :error-message="errors['oidc.timeout_s']" />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import CopyField from '@/components/CopyField.vue'
import { t } from '@/i18n'
import { iconWarning } from '@/utils/icons'

import { CLIENT_AUTH_METHODS, type ProviderFormModel, SIGNING_ALGS } from './providerForm'

defineProps<{ errors: Record<string, string> }>()
const model = defineModel<ProviderFormModel>({ required: true })
const oidc = computed(() => model.value.write.oidc)

// Nullable API fields bound to text inputs.
const secret = computed({ get: () => oidc.value.client_secret ?? '', set: (v: string | null) => (oidc.value.client_secret = v || null) })
const groupsClaim = computed({ get: () => oidc.value.groups_claim ?? '', set: (v: string | null) => (oidc.value.groups_claim = v || null) })
const requireGroup = computed({ get: () => oidc.value.require_group ?? '', set: (v: string | null) => (oidc.value.require_group = v || null) })
const insecure = computed(() => /^http:\/\//i.test(oidc.value.issuer.trim()))

const authOptions = CLIENT_AUTH_METHODS.map((value) => ({ value, label: t(`oidcForm.clientAuthOption.${value}`) }))
</script>

<style scoped src="./section.css"></style>
<style scoped>
.oidc-uris {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.oidc-uris__row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 10px;
  min-width: 0;
}

.oidc-uris__name {
  min-width: 170px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.oidc-toggles {
  display: grid;
  gap: 0;
}

.oidc-toggles__hint {
  margin: -6px 0 6px 50px;
}

.oidc-timeout {
  width: 170px;
}
</style>
