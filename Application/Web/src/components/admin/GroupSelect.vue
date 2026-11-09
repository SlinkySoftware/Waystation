<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Pick local groups (mapped groups come from the provider at sign-in, so they are never picked here). -->
<template>
  <q-select
    v-model="model"
    outlined
    dense
    multiple
    emit-value
    map-options
    use-chips
    options-dense
    :options="options"
    :loading="groups.loading.value"
    :label="label"
    :hint="hint"
    :error="Boolean(error)"
    :error-message="error ?? undefined"
  >
    <template #option="{ opt, itemProps, selected, toggleOption }">
      <q-item v-bind="itemProps" @click="toggleOption(opt)">
        <q-item-section side><q-checkbox :model-value="selected" dense size="sm" @update:model-value="toggleOption(opt)" /></q-item-section>
        <q-item-section>
          <q-item-label>{{ opt.label }}</q-item-label>
          <q-item-label caption>{{ opt.roles }}</q-item-label>
        </q-item-section>
      </q-item>
    </template>
  </q-select>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { api, call } from '@/api/client'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { useDirectoryStore } from '@/stores/directory'

withDefaults(defineProps<{ label: string; hint?: string; error?: string | null }>(), { hint: undefined, error: null })
const model = defineModel<string[]>({ required: true })
const directory = useDirectoryStore()
const groups = useAsync(() => call(api.GET('/groups', { params: { query: { source: 'local' } } })))

const options = computed(() =>
  (groups.data.value ?? []).map((g) => ({
    value: g.id,
    label: g.name,
    roles: g.roles.length ? g.roles.map((r) => directory.roleName(r)).join(', ') : t('admin.roles.none'),
  })),
)
</script>
