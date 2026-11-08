// SPDX-License-Identifier: GPL-3.0-or-later
import { defineRouter } from '#q-app'
import { createRouter, createWebHistory } from 'vue-router'

import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

import { authGuard } from './guard'
import routes from './routes'

export default defineRouter(() => {
  const router = createRouter({
    // BASE_URL is the public path, /ui/ (SPEC §4.4).
    history: createWebHistory(import.meta.env.BASE_URL),
    routes,
    scrollBehavior: (_to, _from, saved) => saved ?? { left: 0, top: 0 },
  })

  router.beforeEach((to) => authGuard(to, useAuthStore()))
  router.afterEach((to) => {
    const title = to.meta.title ? t(to.meta.title) : null
    const repo = typeof to.params.name === 'string' ? to.params.name : null
    document.title = [repo, title, 'Waystation'].filter(Boolean).join(' · ')
  })

  return router
})
