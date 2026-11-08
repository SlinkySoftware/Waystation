<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- LDAP user search, attribute mapping, group discovery, require-group gate and the password-check cache. -->
<template>
  <section class="ldap-section">
    <h3 class="section-label">{{ t('ldapForm.users') }}</h3>
    <q-input v-model="users.base_dn" outlined dense class="mono-input" :label="t('ldapForm.userBase')" :rules="[(v: string) => !!v.trim() || t('ldapForm.required')]" :error="!!errors['ldap.users.base_dn']" :error-message="errors['ldap.users.base_dn']" spellcheck="false" />
    <q-input
      v-model="users.filter"
      outlined
      dense
      class="mono-input"
      :label="t('ldapForm.userFilter')"
      :hint="t('ldapForm.userFilterHint', { attr: users.username_attribute || 'uid' })"
      :rules="[filterRule]"
      :error="!!errors['ldap.users.filter']"
      :error-message="errors['ldap.users.filter']"
      spellcheck="false"
    />
    <div class="ldap-section__row">
      <q-input v-model="users.username_attribute" outlined dense class="col mono-input" :label="t('ldapForm.usernameAttr')" :error="!!errors['ldap.users.username_attribute']" :error-message="errors['ldap.users.username_attribute']" spellcheck="false" />
      <q-input v-model="users.display_name_attribute" outlined dense class="col mono-input" :label="t('ldapForm.displayNameAttr')" :error="!!errors['ldap.users.display_name_attribute']" :error-message="errors['ldap.users.display_name_attribute']" spellcheck="false" />
      <q-input v-model="users.email_attribute" outlined dense class="col mono-input" :label="t('ldapForm.emailAttr')" :error="!!errors['ldap.users.email_attribute']" :error-message="errors['ldap.users.email_attribute']" spellcheck="false" />
    </div>
  </section>

  <section class="ldap-section">
    <h3 class="section-label">{{ t('ldapForm.groups') }}</h3>
    <q-select v-model="groups.mode" outlined dense emit-value map-options :options="modeOptions" :label="t('ldapForm.groupMode')" :error="!!errors['ldap.groups.mode']" :error-message="errors['ldap.groups.mode']" />
    <template v-if="groups.mode !== 'none'">
      <q-input
        v-model="groupBase"
        outlined
        dense
        class="mono-input"
        :label="t('ldapForm.groupBase')"
        :rules="groups.mode === 'search' ? [(v: string) => !!v.trim() || t('ldapForm.required')] : []"
        :error="!!errors['ldap.groups.base_dn']"
        :error-message="errors['ldap.groups.base_dn']"
        spellcheck="false"
      />
      <q-input v-if="groups.mode === 'member_of'" v-model="groups.member_of_attribute" outlined dense class="mono-input" :label="t('ldapForm.memberOfAttr')" :error="!!errors['ldap.groups.member_of_attribute']" :error-message="errors['ldap.groups.member_of_attribute']" spellcheck="false" />
      <q-input v-model="groups.filter" outlined dense class="mono-input" :label="t('ldapForm.groupFilter')" :rules="[filterRule]" :error="!!errors['ldap.groups.filter']" :error-message="errors['ldap.groups.filter']" spellcheck="false" />
      <div class="ldap-section__row">
        <q-input v-model="groups.name_attribute" outlined dense class="col mono-input" :label="t('ldapForm.nameAttr')" :error="!!errors['ldap.groups.name_attribute']" :error-message="errors['ldap.groups.name_attribute']" spellcheck="false" />
        <template v-if="groups.mode === 'search'">
          <q-input v-model="groups.member_attribute" outlined dense class="col mono-input" :label="t('ldapForm.memberAttr')" :error="!!errors['ldap.groups.member_attribute']" :error-message="errors['ldap.groups.member_attribute']" spellcheck="false" />
          <q-select v-model="groups.member_value" outlined dense emit-value map-options class="col" :options="memberValueOptions" :label="t('ldapForm.memberValue')" />
        </template>
      </div>
      <q-toggle v-model="groups.nested" :label="t('ldapForm.nested')" />
      <div class="text-caption text-muted ldap-section__toggle-hint">{{ t('ldapForm.nestedHint') }}</div>
      <q-input
        v-model="requireGroup"
        outlined
        dense
        class="mono-input"
        :label="t('ldapForm.requireGroup')"
        :hint="t('ldapForm.requireGroupHint')"
        :error="!!errors['ldap.groups.require_group']"
        :error-message="errors['ldap.groups.require_group']"
        spellcheck="false"
      />
    </template>
  </section>

  <q-expansion-item dense dense-toggle switch-toggle-side :label="t('ldapForm.advanced')" class="ldap-section__advanced" header-class="section-label">
    <div class="ldap-section ldap-section--inner">
      <q-input
        v-model.number="model.write.ldap.cache_ttl_s"
        outlined
        dense
        type="number"
        min="0"
        max="900"
        :label="t('ldapForm.cacheTtl')"
        :hint="t('ldapForm.cacheTtlHint')"
        :suffix="t('ldapForm.seconds')"
        :error="!!errors['ldap.cache_ttl_s']"
        :error-message="errors['ldap.cache_ttl_s']"
      />
    </div>
  </q-expansion-item>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { t } from '@/i18n'

import type { ProviderFormModel } from './providerForm'

defineProps<{ errors: Record<string, string> }>()
const model = defineModel<ProviderFormModel>({ required: true })
const users = computed(() => model.value.write.ldap.users)
const groups = computed(() => model.value.write.ldap.groups)

const groupBase = computed({ get: () => groups.value.base_dn ?? '', set: (v: string | null) => (groups.value.base_dn = v || null) })
const requireGroup = computed({ get: () => groups.value.require_group ?? '', set: (v: string | null) => (groups.value.require_group = v || null) })

const filterRule = (v: string) => !v.trim() || /^\(.*\)$/s.test(v.trim()) || t('ldapForm.filterInvalid')
const modeOptions = (['member_of', 'search', 'none'] as const).map((value) => ({ value, label: t(`ldapForm.groupModeOption.${value}`) }))
const memberValueOptions = (['dn', 'username'] as const).map((value) => ({ value, label: t(`ldapForm.memberValueOption.${value}`) }))
</script>

<style scoped src="./section.css"></style>
<style scoped>
.ldap-section--inner {
  padding: 10px 0 4px;
}

.ldap-section__advanced :deep(.q-item) {
  padding-left: 0;
}

.ldap-section__toggle-hint {
  margin: -10px 0 0 42px;
}
</style>
