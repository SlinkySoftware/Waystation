<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Effective permissions of a user or service account: its roles and where they come from, global permissions, and
     the allowed actions per repository (a matrix; repositories not listed are hidden from the account). -->
<template>
  <div class="perms">
    <section class="perms__section">
      <h3 class="perms__heading">{{ t('admin.permissions.roles') }}</h3>
      <div v-if="!data.roles.length" class="text-faint">{{ t('admin.roles.none') }}</div>
      <ul v-else class="perms__roles">
        <li v-for="role in data.roles" :key="role.id">
          <span class="perms__role">{{ role.name }}</span>
          <span class="perms__via">{{ role.via.map(viaLabel).join(' · ') }}</span>
        </li>
      </ul>
    </section>

    <section class="perms__section">
      <h3 class="perms__heading">{{ t('admin.permissions.global') }}</h3>
      <div v-if="!data.global_actions.length" class="text-faint">{{ t('admin.permissions.globalNone') }}</div>
      <div v-else class="perms__globals">
        <span v-for="a in data.global_actions" :key="a" class="perms__global" :class="{ 'perms__global--admin': a === 'admin' }">
          <q-icon :name="iconShield" size="14px" />
          {{ t(`admin.action.${a}`) }}
          <q-tooltip>{{ t(`admin.actionHelp.${a}`) }}</q-tooltip>
        </span>
      </div>
    </section>

    <section class="perms__section">
      <div class="perms__repo-head">
        <h3 class="perms__heading">{{ t('admin.permissions.repositories') }} <span class="text-faint num">{{ data.repositories.length }}</span></h3>
        <q-input v-if="data.repositories.length > 8" v-model="filter" dense outlined clearable :placeholder="t('admin.permissions.filter')" class="perms__filter">
          <template #prepend><q-icon :name="iconSearch" size="16px" /></template>
        </q-input>
      </div>
      <div v-if="!data.repositories.length" class="text-faint">{{ t('admin.permissions.repositoriesNone') }}</div>
      <div v-else class="perms__table-wrap">
        <table class="perms__table">
          <thead>
            <tr>
              <th class="perms__name-col">{{ t('admin.permissions.repository') }}</th>
              <th v-for="a in REPO_ACTIONS" :key="a" class="perms__action-col">
                {{ t(`admin.action.${a}`) }}
                <q-tooltip>{{ t(`admin.actionHelp.${a}`) }}</q-tooltip>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="repo in repos" :key="repo.name">
              <td class="perms__name-col"><RepoChip :name="repo.name" :format="repo.format as Format" /></td>
              <td v-for="a in REPO_ACTIONS" :key="a" class="perms__action-col">
                <q-icon v-if="repo.actions.includes(a)" :name="iconCheck" size="16px" class="perms__yes" :aria-label="t(`admin.action.${a}`)" />
                <span v-else class="perms__no" aria-hidden="true">·</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="data.repositories.length" class="perms__note">{{ t('admin.permissions.hiddenNote') }}</div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import type { EffectivePermissions, Format } from '@/api/types'
import RepoChip from '@/components/RepoChip.vue'
import { t } from '@/i18n'
import { iconCheck, iconSearch, iconShield } from '@/utils/icons'
import { REPO_ACTIONS } from '@/utils/rbac'

const props = defineProps<{ data: EffectivePermissions }>()
const filter = ref<string | null>('')

const repos = computed(() => {
  const needle = (filter.value ?? '').trim().toLowerCase()
  return needle ? props.data.repositories.filter((r) => r.name.toLowerCase().includes(needle) || r.format.includes(needle)) : props.data.repositories
})

function viaLabel(source: string): string {
  return source === 'direct' ? t('admin.permissions.direct') : t('admin.permissions.via', { source })
}
</script>

<style scoped>
.perms {
  display: grid;
  gap: 18px;
}

.perms__heading {
  margin: 0 0 8px;
  font-size: var(--fs-13);
  font-weight: 650;
  color: var(--text-secondary);
}

.perms__roles {
  display: grid;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.perms__roles li {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 10px;
}

.perms__role {
  font-weight: 600;
}

.perms__via {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.perms__globals {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.perms__global {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  padding: 0 9px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface-sunken);
  font-size: var(--fs-12);
  font-weight: 600;
}

.perms__global--admin {
  color: var(--status-degraded);
  background: var(--status-degraded-bg);
  border-color: color-mix(in srgb, var(--status-degraded) 40%, transparent);
}

.perms__repo-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.perms__filter {
  width: 220px;
  margin-bottom: 8px;
}

.perms__table-wrap {
  overflow-x: auto;
  max-height: 420px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.perms__table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-13);
}

.perms__table th {
  position: sticky;
  top: 0;
  background: var(--surface);
  font-size: var(--fs-12);
  font-weight: 600;
  color: var(--text-secondary);
  text-align: center;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}

.perms__table td {
  padding: 6px 10px;
  border-bottom: 1px solid var(--border);
  text-align: center;
}

.perms__table tbody tr:last-child td {
  border-bottom: 0;
}

.perms__name-col {
  text-align: left !important;
}

.perms__yes {
  color: var(--status-ok);
}

.perms__no {
  color: var(--text-tertiary);
}

.perms__note {
  margin-top: 6px;
  font-size: var(--fs-12);
  color: var(--text-tertiary);
}
</style>
