<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Pick roles. Roles the caller may not hand out are shown but disabled (a hint; the server refuses them anyway);
     roles already held (`keep`) stay selectable so they can be kept or removed. -->
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
    :label="label"
    :hint="hint"
    :error="Boolean(error)"
    :error-message="error ?? undefined"
    :readonly="readonly"
  >
    <template #option="{ opt, itemProps, selected, toggleOption }">
      <q-item v-bind="itemProps" @click="toggleOption(opt)">
        <q-item-section side><q-checkbox :model-value="selected" dense size="sm" :disable="opt.disable" @update:model-value="toggleOption(opt)" /></q-item-section>
        <q-item-section>
          <q-item-label>
            {{ opt.label }} <span class="role-select__id mono">{{ opt.value }}</span>
          </q-item-label>
          <q-item-label caption>{{ opt.disable ? t('admin.roles.notAssignable') : opt.description }}</q-item-label>
        </q-item-section>
        <q-item-section v-if="opt.global" side>
          <q-icon :name="iconShield" size="16px"><q-tooltip>{{ t('admin.roles.globalWarning') }}</q-tooltip></q-icon>
        </q-item-section>
      </q-item>
    </template>
  </q-select>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'

import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useDirectoryStore } from '@/stores/directory'
import { iconShield } from '@/utils/icons'

import { hasGlobalAction, mayAssignRole } from './forms'

const props = withDefaults(
  defineProps<{ label: string; hint?: string; keep?: string[]; error?: string | null; readonly?: boolean; repositoryOnly?: boolean }>(),
  { hint: undefined, keep: () => [], error: null, readonly: false, repositoryOnly: false },
)
const model = defineModel<string[]>({ required: true })
const auth = useAuthStore()
const directory = useDirectoryStore()

onMounted(() => void directory.loadRoles().catch(() => undefined))

const options = computed(() =>
  directory.roles
    .filter((r) => !props.repositoryOnly || !hasGlobalAction(r))
    .map((r) => ({
      value: r.id,
      label: r.name,
      description: r.description,
      global: hasGlobalAction(r),
      disable: !props.keep.includes(r.id) && !mayAssignRole(r, auth.can),
    })),
)
</script>

<style scoped>
.role-select__id {
  margin-left: 6px;
  font-size: var(--fs-12);
  color: var(--text-tertiary);
}
</style>
