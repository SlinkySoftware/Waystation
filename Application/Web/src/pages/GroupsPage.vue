<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Administration → Groups (UI_DESIGN §4.10, SPEC §4.10): local groups (create, rename, roles, members, delete) and
     groups mapped from sign-in providers (roles only; the provider decides membership at each sign-in). -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.groups')" :caption="t('admin.groups.caption')">
      <template #actions>
        <q-btn unelevated no-caps color="primary" :icon="iconGroupAdd" :label="t('admin.groups.create')" @click="edit(null)" />
      </template>
    </PageHeader>

    <q-card class="groups">
      <q-tabs v-model="tab" dense no-caps align="left" class="groups__tabs" active-color="primary" indicator-color="primary">
        <q-tab name="all" :label="`${t('admin.groups.all')} (${all.length})`" />
        <q-tab name="local" :label="`${t('admin.groups.local')} (${all.filter((g) => g.source === 'local').length})`" />
        <q-tab name="mapped" :label="`${t('admin.groups.mapped')} (${all.filter((g) => g.source !== 'local').length})`" />
      </q-tabs>

      <q-table
        class="ws-table"
        flat
        :rows="rows"
        :columns="columns"
        row-key="id"
        :loading="list.loading.value && !list.data.value"
        :grid="$q.screen.lt.sm"
        :pagination="{ rowsPerPage: 0 }"
        hide-pagination
        @row-click="(_e: Event, row: Group) => edit(row)"
      >
        <template #body-cell-name="{ row }">
          <q-td class="cursor-pointer">
            <GroupName :group="row" />
          </q-td>
        </template>
        <template #body-cell-source="{ row }">
          <q-td class="cursor-pointer"><SourceBadge :source="row.source" /></q-td>
        </template>
        <template #body-cell-roles="{ row }">
          <q-td class="cursor-pointer"><RoleChips :roles="row.roles" /></q-td>
        </template>
        <template #body-cell-members="{ row }">
          <q-td class="num" @click.stop>
            <q-btn v-if="row.source === 'local'" flat dense no-caps :label="String(row.member_count ?? 0)" :icon="iconGroups" @click="showMembers(row)">
              <q-tooltip>{{ t('admin.groups.members') }}</q-tooltip>
            </q-btn>
            <span v-else class="text-faint">{{ t('admin.groups.membersMapped') }}<q-tooltip>{{ t('admin.groups.membersMappedHelp') }}</q-tooltip></span>
          </q-td>
        </template>
        <template #body-cell-updated="{ row }">
          <q-td class="cursor-pointer"><TimeCell :value="row.updated_at" /></q-td>
        </template>
        <template #body-cell-actions="{ row }">
          <q-td class="text-right" @click.stop>
            <q-btn flat round dense size="sm" :icon="iconEdit" :aria-label="`${t('admin.groups.edit')} ${row.name}`" @click="edit(row)" />
            <q-btn v-if="row.source === 'local'" flat round dense size="sm" :icon="iconDelete" :aria-label="`${t('admin.groups.delete')} ${row.name}`" @click="confirmDelete(row)" />
            <q-btn v-else flat round dense size="sm" :icon="iconAuthProviders" :aria-label="t('admin.groups.providerLink')" :to="{ name: 'admin-auth' }">
              <q-tooltip>{{ t('admin.groups.providerLink') }}</q-tooltip>
            </q-btn>
          </q-td>
        </template>

        <template #item="{ row }">
          <div class="groups__card" role="button" tabindex="0" @click="edit(row)" @keydown.enter="edit(row)">
            <div class="groups__card-head">
              <GroupName :group="row" class="col" />
              <SourceBadge :source="row.source" />
            </div>
            <RoleChips :roles="row.roles" />
            <div class="groups__card-actions" @click.stop>
              <q-btn v-if="row.source === 'local'" outline dense no-caps :icon="iconGroups" :label="`${t('admin.groups.members')} (${row.member_count ?? 0})`" @click="showMembers(row)" />
              <q-btn v-if="row.source === 'local'" flat dense no-caps color="negative" :icon="iconDelete" :label="t('admin.groups.delete')" @click="confirmDelete(row)" />
            </div>
          </div>
        </template>

        <template #no-data>
          <EmptyState v-if="list.error.value" class="full-width" illustration="error" :title="t('admin.groups.loadFailed')" :text="errorMessage(list.error.value)">
            <q-btn flat no-caps :label="t('common.retry')" @click="list.reload" />
          </EmptyState>
          <EmptyState v-else-if="!list.loading.value" class="full-width" :icon="iconGroups" :title="t('admin.groups.emptyTitle')" :text="t('admin.groups.emptyText')">
            <q-btn unelevated no-caps color="primary" :label="t('admin.groups.create')" @click="edit(null)" />
          </EmptyState>
        </template>
      </q-table>
    </q-card>

    <GroupDialog v-model="editing" :group="current" @saved="onSaved" />
    <GroupMembersDialog v-model="membersOpen" :group="current" @changed="list.reload" />
  </q-page>
</template>

<script setup lang="ts">
import { type QTableColumn, useQuasar } from 'quasar'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, call, errorMessage } from '@/api/client'
import type { Group } from '@/api/types'
import GroupDialog from '@/components/admin/GroupDialog.vue'
import GroupMembersDialog from '@/components/admin/GroupMembersDialog.vue'
import GroupName from '@/components/admin/GroupName.vue'
import RoleChips from '@/components/admin/RoleChips.vue'
import SourceBadge from '@/components/admin/SourceBadge.vue'
import { useProblemDialog } from '@/components/admin/useAdminActions'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { useDirectoryStore } from '@/stores/directory'
import { iconAuthProviders, iconDelete, iconEdit, iconGroupAdd, iconGroups } from '@/utils/icons'

const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const notify = useNotify()
const showProblem = useProblemDialog()
const directory = useDirectoryStore()
onMounted(() => void directory.loadRoles().catch(() => undefined))

const list = useAsync(() => call(api.GET('/groups')))
const all = computed(() => list.data.value ?? [])
const tabParam = useQueryParam('source')
const tab = computed({
  get: () => (tabParam.value === 'local' || tabParam.value === 'mapped' ? tabParam.value : 'all'),
  set: (v: string) => (tabParam.value = v === 'all' ? null : v),
})
const rows = computed(() => all.value.filter((g) => tab.value === 'all' || (tab.value === 'local') === (g.source === 'local')))

const columns = computed<QTableColumn<Group>[]>(() => [
  { name: 'name', label: t('admin.groups.col.name'), field: 'name', align: 'left', sortable: true },
  { name: 'source', label: t('admin.groups.col.source'), field: 'source', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'roles', label: t('admin.groups.col.roles'), field: (r) => r.roles.join(','), align: 'left' },
  { name: 'members', label: t('admin.groups.col.members'), field: (r) => r.member_count ?? -1, align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'updated', label: t('admin.groups.col.updated'), field: 'updated_at', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'actions', label: '', field: 'id', align: 'right' },
])

const editing = ref(false)
const membersOpen = ref(false)
const current = ref<Group | null>(null)

function edit(group: Group | null): void {
  current.value = group
  editing.value = true
}

function showMembers(group: Group): void {
  current.value = group
  membersOpen.value = true
}

// `?open=<id>` (from a user's page) opens that group: its members for a local group, else its roles.
watch(
  () => [route.query.open, list.data.value] as const,
  ([open, groups]) => {
    if (typeof open !== 'string' || !groups) return
    const group = groups.find((g) => g.id === open)
    if (group) {
      if (group.source === 'local') showMembers(group)
      else edit(group)
    }
    const query = { ...route.query }
    delete query.open
    void router.replace({ query })
  },
  { immediate: true },
)

function onSaved(group: Group): void {
  notify.success(t(current.value ? 'admin.groups.saved' : 'admin.groups.created', { name: group.name }))
  void list.reload()
}

function confirmDelete(group: Group): void {
  $q.dialog({
    title: t('admin.groups.deleteTitle', { name: group.name }),
    message: t('admin.groups.deleteText'),
    cancel: { flat: true, noCaps: true, label: t('common.cancel') },
    ok: { unelevated: true, noCaps: true, color: 'negative', label: t('common.delete') },
    persistent: true,
  }).onOk(async () => {
    try {
      await call(api.DELETE('/groups/{group_id}', { params: { path: { group_id: group.id } } }))
      notify.success(t('admin.groups.deleted', { name: group.name }))
      await list.reload()
    } catch (error) {
      showProblem(error)
    }
  })
}
</script>

<style scoped>
.groups {
  overflow: hidden;
}

.groups__tabs {
  border-bottom: 1px solid var(--border);
  padding: 0 8px;
}

.groups__card {
  width: calc(100% - 24px);
  min-width: 0;
  margin: 0 12px 8px;
  padding: 12px;
  display: grid;
  gap: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  cursor: pointer;
}

.groups__card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.groups__card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
</style>
