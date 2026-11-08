<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- LDAP connection settings: servers (failover order), transport security, custom CA, service bind, timeouts. -->
<template>
  <section class="ldap-section">
    <h3 class="section-label">{{ t('ldapForm.connection') }}</h3>

    <div class="ldap-section__label">{{ t('ldapForm.servers') }}</div>
    <div v-for="(_, i) in conn.servers" :key="i" class="ldap-section__server">
      <span class="ldap-section__index num">{{ i + 1 }}</span>
      <q-input
        v-model="conn.servers[i]"
        outlined
        dense
        class="col mono-input"
        :placeholder="t('ldapForm.serverPlaceholder')"
        :aria-label="t('ldapForm.server', { n: i + 1 })"
        :rules="[(v: string) => !!v.trim() || t('ldapForm.required')]"
        :error="!!errors[`ldap.connection.servers.${i}`]"
        :error-message="errors[`ldap.connection.servers.${i}`]"
        spellcheck="false"
        autocomplete="off"
      />
      <q-btn flat round dense :icon="iconDelete" :disable="conn.servers.length < 2" :aria-label="t('ldapForm.removeServer', { n: i + 1 })" @click="conn.servers.splice(i, 1)" />
    </div>
    <div class="ldap-section__under">
      <q-btn flat dense no-caps color="primary" :icon="iconAdd" :label="t('ldapForm.addServer')" :disable="conn.servers.length >= 8" @click="conn.servers.push('')" />
      <span class="text-caption text-muted">{{ t('ldapForm.serversHint') }}</span>
    </div>
    <div v-if="errors['ldap.connection.servers']" class="ldap-section__error" role="alert">{{ errors['ldap.connection.servers'] }}</div>

    <q-select
      v-model="conn.security"
      outlined
      dense
      emit-value
      map-options
      :options="securityOptions"
      :label="t('ldapForm.security')"
      :error="!!errors['ldap.connection.security']"
      :error-message="errors['ldap.connection.security']"
    />
    <div v-if="conn.security === 'none'" class="ldap-section__warning" role="alert">
      <q-icon :name="iconWarning" size="18px" />
      {{ t('ldapForm.noneWarning') }}
    </div>
    <q-input
      v-if="conn.security !== 'none'"
      v-model="caPem"
      outlined
      dense
      type="textarea"
      autogrow
      class="mono-input"
      :label="t('ldapForm.caPem')"
      :hint="t('ldapForm.caPemHint')"
      placeholder="-----BEGIN CERTIFICATE-----"
      :error="!!errors['ldap.connection.ca_pem']"
      :error-message="errors['ldap.connection.ca_pem']"
      spellcheck="false"
    />

    <q-input
      v-model="bindDn"
      outlined
      dense
      class="mono-input"
      :label="t('ldapForm.bindDn')"
      :hint="t('ldapForm.bindDnHint')"
      :error="!!errors['ldap.connection.bind_dn']"
      :error-message="errors['ldap.connection.bind_dn']"
      spellcheck="false"
      autocomplete="off"
    />
    <q-input
      v-if="bindDn"
      v-model="password"
      outlined
      dense
      type="password"
      autocomplete="new-password"
      :label="t('ldapForm.bindPassword')"
      :hint="model.bindPasswordSet ? t('ldapForm.bindPasswordKept') : t('ldapForm.bindPasswordHint')"
      :error="!!errors['ldap.connection.bind_password']"
      :error-message="errors['ldap.connection.bind_password']"
    />
    <div class="ldap-section__row">
      <q-input v-model.number="conn.connect_timeout_s" outlined dense type="number" min="1" max="30" class="col" :label="t('ldapForm.connectTimeout')" :suffix="t('ldapForm.seconds')" :error="!!errors['ldap.connection.connect_timeout_s']" :error-message="errors['ldap.connection.connect_timeout_s']" />
      <q-input v-model.number="conn.timeout_s" outlined dense type="number" min="1" max="60" class="col" :label="t('ldapForm.timeout')" :suffix="t('ldapForm.seconds')" :error="!!errors['ldap.connection.timeout_s']" :error-message="errors['ldap.connection.timeout_s']" />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { t } from '@/i18n'
import { iconAdd, iconDelete, iconWarning } from '@/utils/icons'

import type { ProviderFormModel } from './providerForm'

defineProps<{ errors: Record<string, string> }>()
const model = defineModel<ProviderFormModel>({ required: true })
const conn = computed(() => model.value.write.ldap.connection)

// Nullable API fields bound to text inputs.
const caPem = computed({ get: () => conn.value.ca_pem ?? '', set: (v: string | null) => (conn.value.ca_pem = v || null) })
const bindDn = computed({ get: () => conn.value.bind_dn ?? '', set: (v: string | null) => (conn.value.bind_dn = v || null) })
const password = computed({ get: () => conn.value.bind_password ?? '', set: (v: string | null) => (conn.value.bind_password = v || null) })

const securityOptions = (['ldaps', 'starttls', 'none'] as const).map((value) => ({ value, label: t(`ldapForm.securityOption.${value}`) }))
</script>

<style scoped src="./section.css"></style>
