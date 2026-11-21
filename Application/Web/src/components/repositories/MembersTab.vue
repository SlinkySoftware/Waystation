<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Repository "Members" (UI_DESIGN §4.3, T-U04): every role that reaches this repository, what it may do here, and
     who holds it (groups, users with the role directly, service accounts, anonymous requests). For user
     administrators; worked out from the role definitions (display only, the server decides each request). -->
<template>
  <div class="members">
    <LoadFailedState v-if="state.error.value && !state.data.value" :error="state.error.value" :title="t('ui.members.loadFailed')" :loading="state.loading.value" @retry="state.reload" />
    <template v-else-if="state.data.value">
      <p class="members__intro">{{ t('ui.members.intro', { name: repo.name }) }}</p>
      <PageCard v-for="r in state.data.value" :key="r.reach.role.id" :title="r.reach.role.name">
        <template #caption>
          <span class="members__scopes">
            <template v-if="r.reach.admin">{{ t('ui.members.viaAdmin') }}</template>
            <template v-else>{{ t('ui.members.via') }} <code v-for="s in r.reach.scopes" :key="s" class="members__scope">{{ s }}</code></template>
          </span>
        </template>
        <template #actions>
          <span v-for="a in r.reach.actions" :key="a" class="ws-badge members__action" :class="`members__action--${a}`">{{ t(`ui.members.action.${a}`) }}</span>
        </template>
        <div class="members__holders">
          <div v-if="r.anonymous" class="members__holder">
            <q-icon :name="iconAnonymous" size="18px" /> <span>{{ t('ui.members.anonymous') }}</span>
          </div>
          <router-link v-for="g in r.groups" :key="g.id" :to="{ name: 'admin-groups' }" class="members__holder">
            <q-icon :name="iconGroups" size="18px" /> <span :class="{ 'mono members__mapped': g.source !== 'local' }">{{ g.source === 'local' ? g.name : (g.external_group ?? g.name) }}</span> <SourceBadge :source="g.source" />
          </router-link>
          <router-link v-for="u in r.users" :key="u.id" :to="{ name: 'admin-user', params: { id: u.id } }" class="members__holder">
            <q-icon :name="iconPerson" size="18px" /> <span>{{ u.display_name || u.username }}</span> <span class="mono text-faint">{{ u.username }}</span>
          </router-link>
          <span v-if="r.moreUsers" class="members__holder members__holder--note">{{ t('ui.members.moreUsers') }}</span>
          <router-link v-for="s in r.accounts" :key="s.id" :to="{ name: 'admin-tokens', query: { tab: 'accounts' } }" class="members__holder">
            <q-icon :name="iconServiceAccount" size="18px" /> <span>{{ s.name }}</span>
          </router-link>
          <span v-if="!r.anonymous && !r.groups.length && !r.users.length && !r.accounts.length" class="members__none">{{ t('ui.members.nobody') }}</span>
        </div>
      </PageCard>
      <EmptyState v-if="!state.data.value.length" compact illustration="notFound" :title="t('ui.members.emptyTitle')" :text="t('ui.members.emptyText')" />
      <div class="members__foot">
        <q-btn flat no-caps :icon="iconRoles" :label="t('ui.members.manageRoles')" :to="{ name: 'admin-roles' }" />
      </div>
    </template>
    <template v-else>
      <q-skeleton v-for="i in 3" :key="i" height="110px" class="q-mb-md" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { api, call } from '@/api/client'
import type { Group, Repository, ServiceAccount, UserSummary } from '@/api/types'
import SourceBadge from '@/components/admin/SourceBadge.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadFailedState from '@/components/LoadFailedState.vue'
import PageCard from '@/components/PageCard.vue'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { iconAnonymous, iconGroups, iconPerson, iconRoles, iconServiceAccount } from '@/utils/icons'

import { type RoleReach, rolesReaching } from './members'

const props = defineProps<{ repo: Repository }>()
const USERS_PER_ROLE = 50

interface RoleHolders {
  reach: RoleReach
  anonymous: boolean
  groups: Group[]
  users: UserSummary[]
  moreUsers: boolean
  accounts: ServiceAccount[]
}

const state = useAsync(async (): Promise<RoleHolders[]> => {
  const [roles, groups, accounts, anonymous] = await Promise.all([
    call(api.GET('/roles')),
    call(api.GET('/groups')),
    call(api.GET('/service-accounts')).catch(() => [] as ServiceAccount[]),
    call(api.GET('/anonymous-access')).catch(() => ({ roles: [] as string[] })),
  ])
  const reaches = rolesReaching(roles, props.repo)
  const users = await Promise.all(reaches.map((r) => call(api.GET('/users', { params: { query: { role: r.role.id, limit: USERS_PER_ROLE } } }))))
  return reaches.map((reach, i) => ({
    reach,
    anonymous: anonymous.roles.includes(reach.role.id),
    groups: groups.filter((g) => g.roles.includes(reach.role.id)),
    users: users[i]!.items,
    moreUsers: users[i]!.next_cursor !== null,
    accounts: accounts.filter((s) => s.roles.includes(reach.role.id)),
  }))
}, [() => props.repo.name])
</script>

<style scoped>
.members {
  display: grid;
  gap: 12px;
}

.members__intro {
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-13);
}

.members__scopes {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}

.members__scope {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--surface-sunken);
  font-family: var(--font-mono);
  font-size: var(--fs-12);
}

.members__action {
  color: var(--text-secondary);
  background: var(--surface-sunken);
}

.members__action--write,
.members__action--delete,
.members__action--manage {
  color: var(--q-primary);
  background: var(--surface-brand);
}

.members__holders {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.members__holder {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-primary);
  text-decoration: none;
  font-size: var(--fs-13);
}

a.members__holder:hover,
a.members__holder:focus-visible {
  border-color: var(--q-primary);
}

.members__mapped {
  max-width: 340px;
  overflow: hidden;
  font-size: var(--fs-12);
  white-space: nowrap;
  text-overflow: ellipsis;
}

.members__holder--note,
.members__none {
  color: var(--text-secondary);
  font-size: var(--fs-13);
}

.members__holder--note {
  border-style: dashed;
}

.members__foot {
  display: flex;
  justify-content: flex-end;
}
</style>
