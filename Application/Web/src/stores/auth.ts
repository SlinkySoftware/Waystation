// SPDX-License-Identifier: GPL-3.0-or-later
// Session state. Capabilities are UI hints only: the server's RBAC engine decides every request (ARCH §7).
import { acceptHMRUpdate, defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api, ApiError, call, setCsrfToken } from '@/api/client'
import type { Session } from '@/api/types'

import { useThemeStore } from './theme'

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

  /** Load the current session once (router guard). A 401 just means "not signed in". */
  async function ensureChecked(): Promise<void> {
    if (checked.value) return
    try {
      adopt(await call(api.GET('/auth/me')))
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) console.warn('Session check failed', error)
      adopt(null)
    } finally {
      checked.value = true
    }
  }

  async function login(username: string, password: string, provider?: string): Promise<void> {
    adopt(await call(api.POST('/auth/login', { body: { username, password, ...(provider ? { provider } : {}) } })))
    checked.value = true
  }

  /** Sign out. Resolves to the identity provider's sign-out page when the session came from an OIDC provider with
   * RP-initiated logout (the caller navigates there), else null. */
  async function logout(): Promise<string | null> {
    try {
      const result = await call(api.POST('/auth/logout'))
      return result?.end_session_url ?? null
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
