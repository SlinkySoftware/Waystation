// SPDX-License-Identifier: GPL-3.0-or-later
// Session state. Capabilities are UI hints only: the server's RBAC engine decides every request (ARCH §7).
import { acceptHMRUpdate, defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api, ApiError, call, setCsrfToken } from '@/api/client'
import type { Session } from '@/api/types'

import { useThemeStore } from './theme'

const MOCK = import.meta.env.WAYSTATION_MOCK === '1'

function hasSessionHint(): boolean {
  if (MOCK || typeof document === 'undefined') return true
  return document.cookie.split(';').some((c) => c.trim().startsWith('ws_csrf='))
}

export const useAuthStore = defineStore('auth', () => {
  const session = ref<Session | null>(null)
  /** True once the initial session check has completed (signed in or not). */
  const checked = ref(false)

  const user = computed(() => session.value?.user ?? null)
  const signedIn = computed(() => session.value !== null)

  function can(capability: string): boolean {
    const caps = session.value?.capabilities ?? []
    return caps.includes('admin') || caps.includes(capability)
  }

  function adopt(next: Session | null): void {
    session.value = next
    setCsrfToken(next?.csrf_token ?? null)
    if (next) useThemeStore().adopt(next.preferences)
  }

  /** Load the current session once (router guard). A 401 just means "not signed in". Without the readable
   * `ws_csrf` cookie (set with the session cookie at sign-in) there is no session to check, so no request is made. */
  async function ensureChecked(): Promise<void> {
    if (checked.value) return
    if (!hasSessionHint()) {
      adopt(null)
      checked.value = true
      return
    }
    try {
      adopt(await call(api.GET('/auth/me')))
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) console.warn('Session check failed', error)
      adopt(null)
    } finally {
      checked.value = true
    }
  }

  async function login(username: string, password: string): Promise<void> {
    adopt(await call(api.POST('/auth/login', { body: { username, password } })))
    checked.value = true
  }

  async function logout(): Promise<void> {
    try {
      await call(api.POST('/auth/logout'))
    } finally {
      adopt(null)
    }
  }

  /** The server said 401 mid-session. */
  function expire(): void {
    adopt(null)
  }

  return { session, checked, user, signedIn, can, ensureChecked, login, logout, expire }
})

if (import.meta.hot) import.meta.hot.accept(acceptHMRUpdate(useAuthStore, import.meta.hot))
