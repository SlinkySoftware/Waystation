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
  }
}

const placeholder = () => import('@/pages/PlaceholderPage.vue')

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
      { path: 'repositories', name: 'repositories', component: () => import('@/pages/RepositoriesPage.vue'), meta: { title: 'nav.repositories' } },
      {
        path: 'repositories/:name/:tab(overview|browse|cleanup|settings)?',
        name: 'repository',
        component: () => import('@/pages/RepositoryDetailPage.vue'),
        meta: { title: 'nav.repositories' },
      },
      { path: 'browse', name: 'browse', component: () => import('@/pages/BrowsePage.vue'), meta: { title: 'nav.browse' } },
      { path: 'search', name: 'search', component: placeholder, meta: { title: 'nav.search', planned: 'T-U06' } },
      { path: 'activity', name: 'activity', component: () => import('@/pages/ActivityPage.vue'), meta: { title: 'nav.activity' } },
      { path: 'storage', name: 'storage', component: () => import('@/pages/StoragePage.vue'), meta: { title: 'nav.storage' } },
      { path: 'cleanup', name: 'cleanup', component: () => import('@/pages/CleanupPage.vue'), meta: { title: 'nav.cleanup' } },
      { path: 'tasks', name: 'tasks', component: placeholder, meta: { title: 'nav.tasks', planned: 'T-O04' } },
      { path: 'admin/users', name: 'admin-users', component: placeholder, meta: { title: 'nav.users', planned: 'T-U08' } },
      { path: 'admin/groups', name: 'admin-groups', component: placeholder, meta: { title: 'nav.groups', planned: 'T-U08' } },
      { path: 'admin/roles', name: 'admin-roles', component: placeholder, meta: { title: 'nav.roles', planned: 'T-U08' } },
      { path: 'admin/auth-providers', name: 'admin-auth', component: () => import('@/pages/AuthProvidersPage.vue'), meta: { title: 'nav.authProviders' } },
      { path: 'admin/tokens', name: 'admin-tokens', component: placeholder, meta: { title: 'nav.apiTokens', planned: 'T-U08' } },
      { path: 'admin/audit', name: 'admin-audit', component: placeholder, meta: { title: 'nav.audit', planned: 'T-U08' } },
      { path: 'admin/settings', name: 'admin-settings', component: placeholder, meta: { title: 'nav.settings', planned: 'T-U08' } },
      { path: 'admin/system', name: 'admin-system', component: placeholder, meta: { title: 'nav.system', planned: 'T-U08' } },
      { path: 'profile', name: 'profile', component: () => import('@/pages/ProfilePage.vue'), meta: { title: 'nav.profile' } },
      { path: ':catchAll(.*)*', name: 'not-found', component: () => import('@/pages/NotFoundPage.vue'), meta: { title: 'notFound.title' } },
    ],
  },
]

export default routes
