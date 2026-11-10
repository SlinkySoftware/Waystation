// SPDX-License-Identifier: GPL-3.0-or-later
// Route-level code splitting: every page is a lazy chunk (ARCH §9).
import type { RouteRecordRaw } from 'vue-router'

import type { MessageKey } from '@/i18n'

declare module 'vue-router' {
  interface RouteMeta {
    /** Reachable without signing in. */
    public?: boolean
    /** Page title key (document title and placeholder pages). */
    title?: MessageKey
    /** Delivery milestone for pages that are not built yet. */
    planned?: string
    /** The navigation item a sub-page belongs to (highlighted in the rail), e.g. a user's page → `admin-users`. */
    nav?: string
    /** The session capability the page needs (`Session.capabilities`; a hint, the server decides). Without it the
     * guard shows the "no access" page instead (SPEC §4.10 "What a user without roles sees"). */
    capability?: string
  }
}

const BROWSE = 'repositories.browse'

const routes: RouteRecordRaw[] = [
  {
    path: '/sign-in',
    component: () => import('@/layouts/BlankLayout.vue'),
    children: [{ path: '', name: 'sign-in', component: () => import('@/pages/SignInPage.vue'), meta: { public: true, title: 'signIn.title' } }],
  },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    children: [
      { path: '', name: 'dashboard', component: () => import('@/pages/DashboardPage.vue'), meta: { title: 'nav.dashboard' } },
      { path: 'repositories', name: 'repositories', component: () => import('@/pages/RepositoriesPage.vue'), meta: { title: 'nav.repositories', capability: BROWSE } },
      { path: 'repositories/import/nexus', name: 'import-nexus', component: () => import('@/pages/NexusImportPage.vue'), meta: { title: 'nexusImport.title', capability: 'admin' } },
      {
        path: 'repositories/:name/:tab(overview|browse|cleanup|settings)?',
        name: 'repository',
        component: () => import('@/pages/RepositoryDetailPage.vue'),
        meta: { title: 'nav.repositories', capability: BROWSE },
      },
      { path: 'browse', name: 'browse', component: () => import('@/pages/BrowsePage.vue'), meta: { title: 'nav.browse', capability: BROWSE } },
      { path: 'search', name: 'search', component: () => import('@/pages/SearchPage.vue'), meta: { title: 'nav.search', capability: BROWSE } },
      { path: 'activity', name: 'activity', component: () => import('@/pages/ActivityPage.vue'), meta: { title: 'nav.activity', capability: 'stats.read' } },
      { path: 'storage', name: 'storage', component: () => import('@/pages/StoragePage.vue'), meta: { title: 'nav.storage', capability: 'stats.read' } },
      { path: 'cleanup', name: 'cleanup', component: () => import('@/pages/CleanupPage.vue'), meta: { title: 'nav.cleanup', capability: 'repositories.manage' } },
      { path: 'tasks', name: 'tasks', component: () => import('@/pages/TasksPage.vue'), meta: { title: 'nav.tasks', capability: 'admin' } },
      { path: 'admin/users', name: 'admin-users', component: () => import('@/pages/UsersPage.vue'), meta: { title: 'nav.users', capability: 'users.admin' } },
      { path: 'admin/users/:id', name: 'admin-user', component: () => import('@/pages/UserDetailPage.vue'), meta: { title: 'nav.users', nav: 'admin-users', capability: 'users.admin' } },
      { path: 'admin/groups', name: 'admin-groups', component: () => import('@/pages/GroupsPage.vue'), meta: { title: 'nav.groups', capability: 'users.admin' } },
      { path: 'admin/roles', name: 'admin-roles', component: () => import('@/pages/RolesPage.vue'), meta: { title: 'nav.roles', capability: 'users.admin' } },
      { path: 'admin/auth-providers', name: 'admin-auth', component: () => import('@/pages/AuthProvidersPage.vue'), meta: { title: 'nav.authProviders', capability: 'settings.admin' } },
      { path: 'admin/tokens', name: 'admin-tokens', component: () => import('@/pages/AdminTokensPage.vue'), meta: { title: 'nav.apiTokens', capability: 'users.admin' } },
      { path: 'admin/audit', name: 'admin-audit', component: () => import('@/pages/AuditLogPage.vue'), meta: { title: 'nav.audit', capability: 'audit.read' } },
      { path: 'admin/webhooks', name: 'admin-webhooks', component: () => import('@/pages/WebhooksPage.vue'), meta: { title: 'nav.webhooks', capability: 'admin' } },
      { path: 'admin/settings', name: 'admin-settings', component: () => import('@/pages/SettingsPage.vue'), meta: { title: 'nav.settings', capability: 'settings.admin' } },
      { path: 'admin/provisioning', name: 'admin-provisioning', component: () => import('@/pages/ProvisioningPage.vue'), meta: { title: 'nav.provisioning', capability: 'admin' } },
      { path: 'admin/system', name: 'admin-system', component: () => import('@/pages/SystemPage.vue'), meta: { title: 'nav.system', capability: 'admin' } },
      { path: 'profile', name: 'profile', component: () => import('@/pages/ProfilePage.vue'), meta: { title: 'nav.profile' } },
      { path: 'no-access', name: 'no-access', component: () => import('@/pages/NoAccessPage.vue'), meta: { title: 'noAccess.title' } },
      { path: ':catchAll(.*)*', name: 'not-found', component: () => import('@/pages/NotFoundPage.vue'), meta: { title: 'notFound.title' } },
    ],
  },
]

export default routes
