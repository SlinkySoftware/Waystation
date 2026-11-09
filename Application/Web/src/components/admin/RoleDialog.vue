<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Create, edit or view a role: name, id (new roles; suggested from the name), description and the permission
     matrix. Built-in roles open read-only. `template` pre-fills a new role (duplicate). -->
<template>
  <FormDialog
    v-model="open"
    :title="title"
    :caption="readonly ? t('admin.roleForm.viewCaption') : role ? t('admin.roleForm.heldBy') : undefined"
    :saving="saving"
    :readonly="readonly"
    :submit-label="role ? t('common.save') : t('admin.rolesPage.create')"
    :submit-disabled="!readonly && invalidMatrix"
    width="860px"
    @submit="save"
  >
    <ProblemBanner :error="problem" />
    <div class="role-form">
      <div class="role-form__row">
        <q-input
          v-model="model.name"
          outlined
          dense
          maxlength="64"
          :readonly="readonly"
          :autofocus="!readonly"
          :label="t('admin.roleForm.name')"
          :rules="[(v: string) => Boolean(v.trim()) || t('admin.roleForm.nameRequired')]"
          :error="Boolean(fieldErrors.name)"
          :error-message="fieldErrors.name"
          @update:model-value="suggestId"
        />
        <q-input
          v-model="model.id"
          outlined
          dense
          maxlength="48"
          input-class="mono"
          :readonly="Boolean(role)"
          :label="t('admin.roleForm.id')"
          :hint="role ? undefined : t('admin.roleForm.idHint')"
          :rules="role ? [] : [(v: string) => ROLE_ID_PATTERN.test(v) || t('admin.roleForm.idInvalid')]"
          :error="Boolean(fieldErrors.id)"
          :error-message="fieldErrors.id"
          @update:model-value="idTouched = true"
        />
      </div>
      <q-input v-model="model.description" outlined dense autogrow maxlength="500" :readonly="readonly" :label="t('admin.roleForm.description')" />
      <PermissionMatrix v-model="model.matrix" :readonly="readonly" :held="role ? toMatrix(role.permissions).global : []" />
      <div v-if="permissionError" class="role-form__error" role="alert">{{ permissionError }}</div>
    </div>
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError, call } from '@/api/client'
import type { Role } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { t } from '@/i18n'
import { toMatrix } from '@/utils/rbac'

import { emptyRoleModel, matrixProblems, modelFromRole, ROLE_ID_PATTERN, type RoleFormModel, slugify, toRoleCreate, toRoleWrite } from './forms'
import PermissionMatrix from './PermissionMatrix.vue'
import ProblemBanner from './ProblemBanner.vue'

const props = withDefaults(defineProps<{ role: Role | null; template?: Role | null }>(), { template: null })
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [role: Role] }>()

const model = ref<RoleFormModel>(emptyRoleModel())
const idTouched = ref(false)
const saving = ref(false)
const problem = ref<unknown>(null)
const fieldErrors = ref<Record<string, string>>({})
const readonly = computed(() => Boolean(props.role?.builtin))
const title = computed(() => {
  if (!props.role) return t('admin.roleForm.createTitle')
  return readonly.value ? t('admin.roleForm.viewTitle', { name: props.role.name }) : t('admin.roleForm.editTitle', { name: props.role.name })
})
const invalidMatrix = computed(() => {
  const p = matrixProblems(model.value.matrix)
  return p.empty || Object.keys(p.rows).length > 0
})
const permissionError = computed(() => Object.entries(fieldErrors.value).find(([k]) => k.startsWith('permissions'))?.[1] ?? null)

watch(open, (isOpen) => {
  if (!isOpen) return
  if (props.role) model.value = modelFromRole(props.role)
  else if (props.template) {
    const copy = modelFromRole(props.template)
    const name = t('admin.rolesPage.copyOf', { name: props.template.name })
    model.value = { ...copy, name, id: slugify(`${props.template.id}-copy`) }
  } else model.value = emptyRoleModel()
  idTouched.value = Boolean(props.template)
  problem.value = null
  fieldErrors.value = {}
})

function suggestId(name: string | number | null): void {
  if (!props.role && !idTouched.value) model.value.id = slugify(String(name ?? ''))
}

async function save(): Promise<void> {
  saving.value = true
  problem.value = null
  fieldErrors.value = {}
  try {
    const role = props.role
    const saved = role
      ? await call(api.PUT('/roles/{role_id}', { params: { path: { role_id: role.id } }, body: toRoleWrite(model.value) }))
      : await call(api.POST('/roles', { body: toRoleCreate(model.value) }))
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
.role-form {
  display: grid;
  gap: 14px;
}

.role-form__row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

@media (max-width: 599px) {
  .role-form__row {
    grid-template-columns: 1fr;
  }
}

.role-form__error {
  font-size: var(--fs-13);
  color: var(--cache-error);
}
</style>
