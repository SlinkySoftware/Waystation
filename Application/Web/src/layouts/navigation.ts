// SPDX-License-Identifier: GPL-3.0-or-later
// Left rail items (UI_DESIGN §3). `capability` hides items the user cannot use (a hint; the server decides); it is
// the same as the route's `meta.capability` (router/routes.ts; navigation.spec.ts keeps them in step).
import type { MessageKey } from '@/i18n'
import {
  iconActivity,
  iconAudit,
  iconAuthProviders,
  iconBrowse,
  iconCleanup,
  iconDashboard,
  iconGroups,
  iconPerson,
  iconProvisioning,
  iconRepositories,
  iconRoles,
  iconSearch,
  iconSettings,
  iconStorage,
  iconSystem,
  iconTasks,
  iconToken,
  iconWebhook,
} from '@/utils/icons'

export interface NavItem {
  label: MessageKey
  icon: string
  to: string
  /** Route name, for active matching. */
  name: string
  capability?: string
  /** Not built yet (placeholder page); shown dimmed with a "soon" tag. */
  planned?: boolean
}

export interface NavGroup {
  label?: MessageKey
  items: NavItem[]
}

export const navigation: NavGroup[] = [
  {
    items: [
      { label: 'nav.dashboard', icon: iconDashboard, to: '/', name: 'dashboard' },
      { label: 'nav.repositories', icon: iconRepositories, to: '/repositories', name: 'repositories', capability: 'repositories.browse' },
      { label: 'nav.browse', icon: iconBrowse, to: '/browse', name: 'browse', capability: 'repositories.browse' },
      { label: 'nav.search', icon: iconSearch, to: '/search', name: 'search', capability: 'repositories.browse' },
      { label: 'nav.activity', icon: iconActivity, to: '/activity', name: 'activity', capability: 'stats.read' },
      { label: 'nav.storage', icon: iconStorage, to: '/storage', name: 'storage', capability: 'stats.read' },
      { label: 'nav.cleanup', icon: iconCleanup, to: '/cleanup', name: 'cleanup', capability: 'repositories.manage' },
      { label: 'nav.tasks', icon: iconTasks, to: '/tasks', name: 'tasks', capability: 'admin' },
    ],
  },
  {
    label: 'nav.administration',
    items: [
      { label: 'nav.users', icon: iconPerson, to: '/admin/users', name: 'admin-users', capability: 'users.admin' },
      { label: 'nav.groups', icon: iconGroups, to: '/admin/groups', name: 'admin-groups', capability: 'users.admin' },
      { label: 'nav.roles', icon: iconRoles, to: '/admin/roles', name: 'admin-roles', capability: 'users.admin' },
      { label: 'nav.authProviders', icon: iconAuthProviders, to: '/admin/auth-providers', name: 'admin-auth', capability: 'settings.admin' },
      { label: 'nav.apiTokens', icon: iconToken, to: '/admin/tokens', name: 'admin-tokens', capability: 'users.admin' },
      { label: 'nav.audit', icon: iconAudit, to: '/admin/audit', name: 'admin-audit', capability: 'audit.read' },
      { label: 'nav.webhooks', icon: iconWebhook, to: '/admin/webhooks', name: 'admin-webhooks', capability: 'admin' },
      { label: 'nav.settings', icon: iconSettings, to: '/admin/settings', name: 'admin-settings', capability: 'settings.admin' },
      { label: 'nav.provisioning', icon: iconProvisioning, to: '/admin/provisioning', name: 'admin-provisioning', capability: 'admin' },
      { label: 'nav.system', icon: iconSystem, to: '/admin/system', name: 'admin-system', capability: 'admin' },
    ],
  },
]

/** The rail for a session: items whose capability it holds, groups that keep at least one item. */
export function visibleNavigation(can: (capability: string) => boolean, groups: NavGroup[] = navigation): NavGroup[] {
  return groups.map((g) => ({ ...g, items: g.items.filter((i) => !i.capability || can(i.capability)) })).filter((g) => g.items.length)
}
