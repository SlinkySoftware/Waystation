<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Administration → Users (UI_DESIGN §4.10, SPEC §4.10): every account with its source, roles, last sign-in and
     state; filters in the URL; keyset paging; row actions (edit, reset password, unlock, disable, delete). -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.users')" :caption="t('admin.users.caption')">
      <template #actions>
        <q-btn unelevated no-caps color="primary" :icon="iconPersonAdd" :label="t('admin.users.create')" @click="edit(null)" />
      </template>
    </PageHeader>

    <q-card class="users">
      <div class="users__toolbar">
        <q-input v-model="search" dense outlined clearable debounce="250" :placeholder="t('admin.users.search')" class="users__search">
          <template #prepend><q-icon :name="iconSearch" size="18px" /></template>
        </q-input>
        <q-select v-model="source" dense outlined emit-value map-options options-dense :options="sourceOptions" class="users__filter" :aria-label="t('admin.users.col.source')" />
        <q-select v-model="status" dense outlined emit-value map-options options-dense :options="statusOptions" class="users__filter" :aria-label="t('admin.users.col.status')" />
        <q-select v-model="role" dense outlined emit-value map-options options-dense clearable :options="roleOptions" :label="t('admin.users.role')" class="users__filter" />
      </div>

      <q-table
        class="ws-table"
        flat
        :rows="list.items.value"
        :columns="columns"
        row-key="id"
        :loading="list.loading.value && !list.items.value.length"
        :grid="$q.screen.lt.sm"
        :pagination="{ rowsPerPage: 0 }"
        hide-pagination
        @row-click="(_e: Event, row: UserSummary) => open(row)"
      >
        <template #body-cell-user="{ row }">
          <q-td class="cursor-pointer">
            <UserCell :user="row" :self="row.id === auth.user?.id" />
          </q-td>
        </template>
        <template #body-cell-source="{ row }">
          <q-td class="cursor-pointer"><SourceBadge :source="row.source" /></q-td>
        </template>
        <template #body-cell-roles="{ row }">
          <q-td class="cursor-pointer"><RoleChips :roles="row.roles" :empty="'–'" /></q-td>
        </template>
        <template #body-cell-groups="{ row }">
          <q-td class="cursor-pointer num text-muted">{{ row.groups.length ? t('admin.users.groupCount', { count: row.groups.length }) : '–' }}</q-td>
        </template>
        <template #body-cell-lastLogin="{ row }">
          <q-td class="cursor-pointer"><TimeCell :value="row.last_login" :placeholder="t('admin.users.neverSignedIn')" /></q-td>
        </template>
        <template #body-cell-status="{ row }">
          <q-td class="cursor-pointer"><UserStatus :user="row" /></q-td>
        </template>
        <template #body-cell-actions="{ row }">
          <q-td class="text-right" @click.stop>
            <UserRowMenu :user="row" @edit="editRow(row)" @reset="resetting = row" @changed="list.reload" @deleted="list.reload" />
          </q-td>
        </template>

        <template #item="{ row }">
          <div class="users__card" role="button" tabindex="0" @click="open(row)" @keydown.enter="open(row)">
            <div class="users__card-head">
              <UserCell :user="row" :self="row.id === auth.user?.id" class="col" />
              <span @click.stop><UserRowMenu :user="row" @edit="editRow(row)" @reset="resetting = row" @changed="list.reload" @deleted="list.reload" /></span>
            </div>
            <div class="users__card-meta">
              <SourceBadge :source="row.source" />
              <UserStatus :user="row" />
              <RoleChips :roles="row.roles" :empty="''" />
              <span class="text-muted">{{ t('admin.users.col.lastLogin') }}: <TimeCell :value="row.last_login" :placeholder="t('admin.users.neverSignedIn')" /></span>
            </div>
          </div>
        </template>

        <template #no-data>
          <EmptyState v-if="list.error.value" class="full-width" illustration="error" :title="t('admin.users.loadFailed')" :text="errorMessage(list.error.value)">
            <q-btn flat no-caps :label="t('common.retry')" @click="list.reload" />
          </EmptyState>
          <EmptyState v-else-if="!list.loading.value && filtered" class="full-width" illustration="search" :title="t('admin.users.noMatch')" :text="t('admin.users.noMatchText')">
            <q-btn flat no-caps :label="t('admin.users.clearFilters')" @click="clearFilters" />
          </EmptyState>
          <EmptyState v-else-if="!list.loading.value" class="full-width" :icon="iconPerson" :title="t('admin.users.emptyTitle')" :text="t('admin.users.emptyText')">
            <q-btn unelevated no-caps color="primary" :label="t('admin.users.create')" @click="edit(null)" />
          </EmptyState>
        </template>
      </q-table>

      <footer v-if="list.items.value.length" class="users__footer">
        <span class="text-faint">{{ list.nextCursor.value ? t('admin.users.shownMore', { count: list.items.value.length }) : t('admin.users.shown', { count: list.items.value.length }) }}</span>
        <q-btn v-if="list.nextCursor.value" outline no-caps :loading="list.loading.value" :label="t('common.loadMore')" @click="list.loadMore" />
      </footer>
    </q-card>

    <UserDialog v-model="editing" :user="current" @saved="onSaved" />
    <ResetPasswordDialog :model-value="resetting !== null" :user="resetting" @update:model-value="(v: boolean) => !v && (resetting = null)" />
  </q-page>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { api, call, errorMessage } from '@/api/client'
import type { UserDetail, UserSummary } from '@/api/types'
import ResetPasswordDialog from '@/components/admin/ResetPasswordDialog.vue'
import RoleChips from '@/components/admin/RoleChips.vue'
import SourceBadge from '@/components/admin/SourceBadge.vue'
import { useProblemDialog } from '@/components/admin/useAdminActions'
import UserCell from '@/components/admin/UserCell.vue'
import UserDialog from '@/components/admin/UserDialog.vue'
import UserRowMenu from '@/components/admin/UserRowMenu.vue'
import UserStatus from '@/components/admin/UserStatus.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useCursorList } from '@/composables/useCursorList'
import { useNotify } from '@/composables/useNotify'
import { useQueryNavigate, useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useDirectoryStore } from '@/stores/directory'
import { iconPerson, iconPersonAdd, iconSearch } from '@/utils/icons'

const PAGE = 50
const $q = useQuasar()
const router = useRouter()
const notify = useNotify()
const showProblem = useProblemDialog()
const auth = useAuthStore()
const directory = useDirectoryStore()
onMounted(() => {
  void directory.loadRoles().catch(() => undefined)
  void directory.loadProviders()
})

const search = useQueryParam('q')
const source = useQueryParam('source')
const status = useQueryParam('status')
const role = useQueryParam('role')
const navigate = useQueryNavigate()
const filtered = computed(() => Boolean(search.value || source.value || status.value || role.value))

function clearFilters(): void {
  navigate({ q: null, source: null, status: null, role: null })
}

const list = useCursorList(
  (cursor) =>
    call(
      api.GET('/users', {
        params: {
          query: {
            q: search.value?.trim().toLowerCase() || undefined,
            source: source.value ?? undefined,
            disabled: status.value === 'disabled' ? true : status.value === 'enabled' ? false : undefined,
            role: role.value ?? undefined,
            cursor: cursor ?? undefined,
            limit: PAGE,
          },
        },
      }),
    ),
  [search, source, status, role],
)

const sourceOptions = computed(() => [
  { value: null, label: t('admin.users.allSources') },
  ...(['local', 'ldap', 'oidc'] as const).map((kind) => ({ value: kind, label: t(`admin.source.${kind}`) })),
  ...directory.providers.map((p) => ({ value: `${p.kind}:${p.id}`, label: `${p.kind.toUpperCase()} · ${p.name}` })),
])
const statusOptions = computed(() => [
  { value: null, label: t('admin.users.allStates') },
  { value: 'enabled', label: t('admin.users.enabledOnly') },
  { value: 'disabled', label: t('admin.users.disabledOnly') },
])
const roleOptions = computed(() => directory.roles.map((r) => ({ value: r.id, label: r.name })))

const columns = computed<QTableColumn<UserSummary>[]>(() => [
  { name: 'user', label: t('admin.users.col.user'), field: 'username', align: 'left' },
  { name: 'source', label: t('admin.users.col.source'), field: 'source', align: 'left' },
  { name: 'roles', label: t('admin.users.col.roles'), field: (r) => r.roles.join(','), align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'groups', label: t('admin.users.col.groups'), field: (r) => r.groups.length, align: 'left', classes: 'gt-md', headerClasses: 'gt-md' },
  { name: 'lastLogin', label: t('admin.users.col.lastLogin'), field: (r) => r.last_login ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'status', label: t('admin.users.col.status'), field: (r) => (r.disabled ? 'disabled' : r.locked ? 'locked' : 'active'), align: 'left' },
  { name: 'actions', label: '', field: 'id', align: 'right' },
])

function open(row: UserSummary): void {
  void router.push({ name: 'admin-user', params: { id: row.id } })
}

const editing = ref(false)
const current = ref<UserDetail | null>(null)
const resetting = ref<UserSummary | null>(null)

function edit(user: UserDetail | null): void {
  current.value = user
  editing.value = true
}

/** The edit form needs the full user (identities, has_password). */
async function editRow(row: UserSummary): Promise<void> {
  try {
    edit(await call(api.GET('/users/{user_id}', { params: { path: { user_id: row.id } } })))
  } catch (error) {
    showProblem(error)
  }
}

function onSaved(user: UserDetail): void {
  notify.success(t(current.value ? 'admin.users.saved' : 'admin.users.created', { name: user.username }))
  void list.reload()
}
</script>

<style scoped>
.users {
  overflow: hidden;
}

.users__toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.users__search {
  flex: 1 1 240px;
  max-width: 360px;
}

.users__filter {
  flex: 0 1 180px;
  min-width: 140px;
}

.users__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-top: 1px solid var(--border);
  font-size: var(--fs-13);
}

.users__card {
  width: calc(100% - 24px);
  min-width: 0;
  margin: 0 12px 8px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  cursor: pointer;
}

.users__card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.users__card-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  margin-top: 10px;
  font-size: var(--fs-12);
}
</style>
