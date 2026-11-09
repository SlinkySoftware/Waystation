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
  /** The account holds a one-time password: the server allows only changing it (and signing out). */
  const mustChangePassword = computed(() => session.value?.must_change_password ?? false)
  /** Policy requires two-factor sign-in for this account and it is not set up: the server allows only that. */
  const totpSetupRequired = computed(() => session.value?.totp_setup_required ?? false)

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

  /** Password step. Resolves to 'signed-in', or 'second-factor' when the account has two-factor sign-in (the
   * server then holds a pending sign-in for a few minutes; finish it with `loginSecondFactor`). */
  async function login(username: string, password: string, provider?: string): Promise<'signed-in' | 'second-factor'> {
    const result = await call(api.POST('/auth/login', { body: { username, password, ...(provider ? { provider } : {}) } }))
    if ('second_factor' in result) return 'second-factor'
    adopt(result)
    checked.value = true
    return 'signed-in'
  }

  /** Second step: a code from the authenticator app or a recovery code. */
  async function loginSecondFactor(code: string): Promise<void> {
    adopt(await call(api.POST('/auth/login/totp', { body: { code } })))
    checked.value = true
  }

  /** Reload the session (after enrolling a second factor, which clears `totp_setup_required`). */
  async function refresh(): Promise<void> {
    adopt(await call(api.GET('/auth/me')))
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

  /** Change the caller's local password, then reload the session (clears `must_change_password`). */
  async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
    await call(api.POST('/auth/me/password', { body: { current_password: currentPassword, new_password: newPassword } }))
    adopt(await call(api.GET('/auth/me')))
  }

  /** The server said 401 mid-session. */
  function expire(): void {
    adopt(null)
  }

  return { session, checked, user, signedIn, mustChangePassword, totpSetupRequired, can, ensureChecked, login, loginSecondFactor, refresh, logout, changePassword, expire }
})

if (import.meta.hot) import.meta.hot.accept(acceptHMRUpdate(useAuthStore, import.meta.hot))
