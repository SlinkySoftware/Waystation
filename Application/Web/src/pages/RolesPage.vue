<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Administration → Roles (UI_DESIGN §4.10, SPEC §4.10): built-in roles (read-only) and custom roles with their
     permissions and who holds them; the permission matrix editor; anonymous access. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.roles')" :caption="t('admin.rolesPage.caption')">
      <template #actions>
        <q-btn unelevated no-caps color="primary" :icon="iconAdd" :label="t('admin.rolesPage.create')" @click="open(null)" />
      </template>
    </PageHeader>

    <div class="roles">
      <div v-if="list.error.value && !list.data.value" class="roles__error" role="alert">
        {{ t('admin.rolesPage.loadFailed') }}: {{ errorMessage(list.error.value) }}
        <q-btn flat dense no-caps :label="t('common.retry')" @click="list.reload" />
      </div>
      <div v-else class="roles__grid">
        <q-skeleton v-for="n in list.data.value ? 0 : 4" :key="n" height="150px" />
        <article v-for="role in list.data.value ?? []" :key="role.id" class="role-card q-card" role="button" tabindex="0" @click="open(role)" @keydown.enter="open(role)">
          <header class="role-card__head">
            <q-icon :name="role.builtin ? iconLock : iconRoles" size="20px" class="role-card__icon" />
            <div class="col">
              <div class="role-card__name">{{ role.name }} <ManagedBadge :item="role" /></div>
              <div class="role-card__id mono">{{ role.id }}</div>
            </div>
            <span class="role-card__kind" :class="{ 'role-card__kind--builtin': role.builtin }">{{ role.builtin ? t('admin.roles.builtin') : t('admin.roles.custom') }}</span>
            <q-btn flat round dense size="sm" :icon="iconMore" :aria-label="role.name" @click.stop>
              <q-menu auto-close anchor="bottom right" self="top right">
                <q-list dense style="min-width: 180px">
                  <q-item clickable @click="open(role)">
                    <q-item-section side><q-icon :name="role.builtin || isManaged(role) ? iconShow : iconEdit" size="18px" /></q-item-section>
                    <q-item-section>{{ role.builtin || isManaged(role) ? t('admin.rolesPage.view') : t('admin.rolesPage.edit') }}</q-item-section>
                  </q-item>
                  <q-item clickable @click="duplicate(role)">
                    <q-item-section side><q-icon :name="iconCopy" size="18px" /></q-item-section>
                    <q-item-section>{{ t('admin.rolesPage.duplicate') }}</q-item-section>
                  </q-item>
                  <q-item v-if="!role.builtin && !isManaged(role)" clickable class="text-negative" @click="confirmDelete(role)">
                    <q-item-section side><q-icon :name="iconDelete" size="18px" color="negative" /></q-item-section>
                    <q-item-section>{{ t('admin.rolesPage.delete') }}</q-item-section>
                  </q-item>
                </q-list>
              </q-menu>
            </q-btn>
          </header>
          <p class="role-card__desc">{{ role.description }}</p>
          <ul class="role-card__perms">
            <li v-for="line in summary(role)" :key="line.key">
              <span class="role-card__actions">{{ line.actions }}</span>
              <span v-if="line.scope" class="role-card__scope mono">{{ line.scope }}</span>
            </li>
          </ul>
          <footer class="role-card__foot">
            <span class="text-faint">{{ t('admin.rolesPage.permissionCount', { count: role.permissions.length }) }}</span>
            <span v-if="!role.builtin" class="text-muted">{{ heldBy(role.id) }}</span>
          </footer>
        </article>
      </div>

      <AnonymousAccessCard />
    </div>

    <RoleDialog v-model="editing" :role="current" :template="template" @saved="onSaved" />
  </q-page>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { onMounted, reactive, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { Role, RoleUsage } from '@/api/types'
import AnonymousAccessCard from '@/components/admin/AnonymousAccessCard.vue'
import RoleDialog from '@/components/admin/RoleDialog.vue'
import { useProblemDialog } from '@/components/admin/useAdminActions'
import ManagedBadge from '@/components/ManagedBadge.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useDirectoryStore } from '@/stores/directory'
import { iconAdd, iconCopy, iconDelete, iconEdit, iconLock, iconMore, iconRoles, iconShow } from '@/utils/icons'
import { isManaged } from '@/utils/managed'
import { toMatrix } from '@/utils/rbac'

const $q = useQuasar()
const notify = useNotify()
const showProblem = useProblemDialog()
const directory = useDirectoryStore()
const list = useAsync(() => call(api.GET('/roles')))
const usage = reactive<Record<string, RoleUsage>>({})

// "Held by" for custom roles (built-in roles report no usage).
watch(list.data, (roles) => {
  for (const role of roles ?? []) {
    if (role.builtin) continue
    void call(api.GET('/roles/{role_id}', { params: { path: { role_id: role.id } } }))
      .then((detail) => (usage[role.id] = detail.usage))
      .catch(() => undefined)
  }
})
onMounted(() => void directory.loadRoles().catch(() => undefined))

function heldBy(id: string): string {
  const u = usage[id]
  if (!u) return ''
  if (!u.users && !u.groups && !u.service_accounts) return t('admin.rolesPage.heldByNone')
  return `${t('admin.rolesPage.heldBy')}: ${t('admin.rolesPage.heldByValue', { users: u.users, groups: u.groups, accounts: u.service_accounts })}`
}

/** One line per scope ("Browse, Read · *") then the global actions. */
function summary(role: Role): { key: string; actions: string; scope: string | null }[] {
  const m = toMatrix(role.permissions)
  const lines: { key: string; actions: string; scope: string | null }[] = m.rows.map((r) => ({ key: r.scope, actions: r.actions.map((a) => t(`admin.action.${a}`)).join(', '), scope: r.scope }))
  if (m.global.length) lines.push({ key: '#global', actions: m.global.map((a) => t(`admin.action.${a}`)).join(', '), scope: null })
  return lines
}

const editing = ref(false)
const current = ref<Role | null>(null)
const template = ref<Role | null>(null)

function open(role: Role | null): void {
  current.value = role
  template.value = null
  editing.value = true
}

function duplicate(role: Role): void {
  current.value = null
  template.value = role
  editing.value = true
}

async function refresh(): Promise<void> {
  await list.reload()
  await directory.loadRoles(true).catch(() => undefined)
}

function onSaved(role: Role): void {
  notify.success(t(current.value ? 'admin.rolesPage.saved' : 'admin.rolesPage.created', { name: role.name }))
  void refresh()
}

function confirmDelete(role: Role): void {
  $q.dialog({
    title: t('admin.rolesPage.deleteTitle', { name: role.name }),
    message: `${t('admin.rolesPage.deleteText')} ${heldBy(role.id)}`.trim(),
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'negative', label: t('common.delete') },
    persistent: true,
  }).onOk(async () => {
    try {
      await call(api.DELETE('/roles/{role_id}', { params: { path: { role_id: role.id } } }))
      notify.success(t('admin.rolesPage.deleted', { name: role.name }))
      await refresh()
    } catch (error) {
      showProblem(error)
    }
  })
}
</script>

<style scoped>
.roles {
  display: grid;
  gap: 16px;
}

.roles__error {
  padding: 16px;
  color: var(--cache-error);
}

.roles__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}

.role-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px 16px;
  cursor: pointer;
  transition: border-color 150ms ease-out;
}

.role-card:hover,
.role-card:focus-visible {
  border-color: var(--q-primary);
}

.role-card__head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.role-card__icon {
  color: var(--q-primary);
}

.role-card__name {
  font-weight: 650;
}

.role-card__id {
  font-size: var(--fs-12);
  color: var(--text-tertiary);
}

.role-card__kind {
  padding: 1px 7px;
  border: 1px solid var(--border-strong);
  border-radius: 4px;
  font-size: 11px;
  font-weight: 650;
  color: var(--text-secondary);
}

.role-card__kind--builtin {
  background: var(--surface-sunken);
}

.role-card__desc {
  margin: 0;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.role-card__perms {
  display: grid;
  gap: 3px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: var(--fs-13);
}

.role-card__perms li {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.role-card__actions {
  font-weight: 550;
}

.role-card__scope {
  padding: 0 6px;
  border-radius: 4px;
  background: var(--surface-sunken);
  font-size: var(--fs-12);
}

.role-card__foot {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 4px 12px;
  margin-top: auto;
  padding-top: 6px;
  border-top: 1px solid var(--border);
  font-size: var(--fs-12);
}
</style>
