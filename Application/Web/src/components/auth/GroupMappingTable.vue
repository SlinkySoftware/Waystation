<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Directory group → Waystation roles. Rows match a group by DN or name, case-insensitively. -->
<template>
  <section class="ldap-section">
    <h3 class="section-label">{{ t('ldapForm.mapping') }}</h3>
    <div class="text-caption text-muted">{{ t('ldapForm.mappingHint') }}</div>

    <div v-if="rows.length" class="mapping" role="table" :aria-label="t('ldapForm.mapping')">
      <div class="mapping__head" role="row">
        <span role="columnheader">{{ t('ldapForm.externalGroup') }}</span>
        <span role="columnheader">{{ t('ldapForm.roles') }}</span>
        <span />
      </div>
      <div v-for="(row, i) in rows" :key="i" class="mapping__row" role="row">
        <q-input
          v-model="row.external_group"
          outlined
          dense
          class="mono-input"
          role="cell"
          :placeholder="t('ldapForm.externalGroupPlaceholder')"
          :aria-label="`${t('ldapForm.externalGroup')} ${i + 1}`"
          :error="!!errors[`group_mapping.${i}.external_group`]"
          :error-message="errors[`group_mapping.${i}.external_group`]"
          spellcheck="false"
        />
        <q-select
          v-model="row.roles"
          outlined
          dense
          multiple
          use-chips
          emit-value
          map-options
          options-dense
          role="cell"
          :options="roleOptions"
          :loading="roles.loading.value"
          :aria-label="`${t('ldapForm.roles')} ${i + 1}`"
          :error="!!errors[`group_mapping.${i}.roles`]"
          :error-message="errors[`group_mapping.${i}.roles`]"
        >
          <template #option="{ opt, itemProps, selected, toggleOption }">
            <q-item v-bind="itemProps">
              <q-item-section side><q-checkbox :model-value="selected" dense @update:model-value="toggleOption(opt)" /></q-item-section>
              <q-item-section>
                <q-item-label>{{ opt.label }}</q-item-label>
                <q-item-label caption>{{ opt.description }}</q-item-label>
              </q-item-section>
            </q-item>
          </template>
        </q-select>
        <q-btn flat round dense :icon="iconDelete" :aria-label="t('ldapForm.removeMapping')" @click="rows.splice(i, 1)" />
      </div>
    </div>
    <div v-else class="mapping__empty text-caption">{{ t('ldapForm.noMappings') }}</div>
    <div>
      <q-btn flat dense no-caps color="primary" :icon="iconAdd" :label="t('ldapForm.addMapping')" :disable="rows.length >= 500" @click="rows.push({ external_group: '', roles: [] })" />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { api, call } from '@/api/client'
import type { GroupMapping, Role } from '@/api/types'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { iconAdd, iconDelete } from '@/utils/icons'

defineProps<{ errors: Record<string, string> }>()
const rows = defineModel<GroupMapping[]>({ required: true })

const roles = useAsync<Role[]>(() => call(api.GET('/roles')))
const roleOptions = computed(() => (roles.data.value ?? []).map((r) => ({ value: r.id, label: r.name, description: r.description })))
</script>

<style scoped src="./section.css"></style>
<style scoped>
.mapping {
  display: grid;
  gap: 6px;
}

.mapping__head,
.mapping__row {
  display: grid;
  grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr) 36px;
  gap: 8px;
  align-items: start;
}

.mapping__head {
  font-size: var(--fs-12);
  font-weight: 600;
  color: var(--text-secondary);
}

.mapping__empty {
  padding: 10px 12px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
}

@media (max-width: 599px) {
  .mapping__head {
    display: none;
  }

  .mapping__row {
    grid-template-columns: minmax(0, 1fr) 36px;
  }

  .mapping__row > :nth-child(2) {
    grid-row: 2;
  }
}
</style>
