<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Create or edit a local group (name, description, roles), or set the roles of a group mapped from a provider (its
     provider and external group are shown read-only). -->
<template>
  <FormDialog
    v-model="open"
    :title="title"
    :caption="managed ? t('provisioning.managedHelp') : mapped ? t('admin.groupForm.mappedCaption') : undefined"
    :readonly="managed"
    :saving="saving"
    :submit-label="group ? t('common.save') : t('admin.groups.create')"
    @submit="save"
  >
    <ProblemBanner :error="problem" />
    <div class="group-form">
      <template v-if="mapped && group">
        <dl class="group-form__facts">
          <div><dt>{{ t('admin.groupForm.provider') }}</dt><dd>{{ group.provider?.name ?? group.provider?.id }} <SourceBadge :source="group.source" /></dd></div>
          <div><dt>{{ t('admin.groupForm.externalGroup') }}</dt><dd class="mono">{{ group.external_group }}</dd></div>
        </dl>
      </template>
      <template v-else>
        <q-input
          v-model="model.name"
          outlined
          dense
          autofocus
          :readonly="managed"
          maxlength="64"
          :label="t('admin.groupForm.name')"
          :hint="t('admin.groupForm.nameHint')"
          :rules="[(v: string) => NAME.test(v.trim()) || t('admin.groupForm.nameInvalid')]"
          :error="Boolean(fieldErrors.name)"
          :error-message="fieldErrors.name"
        />
        <q-input v-model="model.description" outlined dense autogrow maxlength="500" :readonly="managed" :label="t('admin.groupForm.description')" />
      </template>
      <RoleSelect v-model="model.roles" :readonly="managed" :label="t('admin.groupForm.roles')" :hint="t('admin.groupForm.rolesHint')" :keep="group?.roles ?? []" :error="rolesError" />
    </div>
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call } from '@/api/client'
import type { Group } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { t } from '@/i18n'
import { isManaged } from '@/utils/managed'

import { type GroupFormModel, toGroupCreate, toGroupUpdate } from './forms'
import ProblemBanner from './ProblemBanner.vue'
import RoleSelect from './RoleSelect.vue'
import SourceBadge from './SourceBadge.vue'

const props = defineProps<{ group: Group | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [group: Group] }>()

const NAME = /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$/
const model = ref<GroupFormModel>({ name: '', description: '', roles: [] })
const saving = ref(false)
const problem = ref<unknown>(null)
const fieldErrors = ref<Record<string, string>>({})
const mapped = computed(() => Boolean(props.group && props.group.source !== 'local'))
const managed = computed(() => isManaged(props.group))
const title = computed(() => (mapped.value ? t('admin.groupForm.mappedTitle') : props.group ? t('admin.groupForm.editTitle', { name: props.group.name }) : t('admin.groupForm.createTitle')))
const rolesError = computed(() => Object.entries(fieldErrors.value).find(([k]) => k.startsWith('roles'))?.[1] ?? null)

watch(open, (isOpen) => {
  if (!isOpen) return
  const g = props.group
  model.value = { name: g?.name ?? '', description: g?.description ?? '', roles: [...(g?.roles ?? [])] }
  problem.value = null
  fieldErrors.value = {}
})

async function save(): Promise<void> {
  saving.value = true
  problem.value = null
  fieldErrors.value = {}
  try {
    const g = props.group
    const saved = g
      ? await call(api.PATCH('/groups/{group_id}', { params: { path: { group_id: g.id } }, body: toGroupUpdate(model.value, g) }))
      : await call(api.POST('/groups', { body: toGroupCreate(model.value) }))
    open.value = false
    emit('saved', saved)
  } catch (error) {
    if (error instanceof ApiError) fieldErrors.value = error.fieldErrors
    problem.value = error
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.group-form {
  display: grid;
  gap: 14px;
}

.group-form__facts {
  display: grid;
  gap: 10px;
  margin: 0;
}

.group-form__facts dt {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.group-form__facts dd {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 2px 0 0;
  overflow-wrap: anywhere;
}
</style>
