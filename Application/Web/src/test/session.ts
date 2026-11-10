// SPDX-License-Identifier: GPL-3.0-or-later
// Test sessions by capability set (SPEC §4.10): what the server's `/auth/me` answers for typical principals.
import type { Session } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

/** Capabilities of the minimal principals the UI must handle calmly. */
export const CAPS = {
  /** Signed in (e.g. in the provider's required group) without any role. */
  noRoles: [] as string[],
  /** The built-in `reader` role, or the anonymous-access grants (browse + read on proxies): no global action. */
  reader: ['repositories.browse'],
  /** The built-in `viewer` role. */
  viewer: ['repositories.browse', 'stats.read'],
  admin: ['admin', 'repositories.create', 'repositories.manage', 'repositories.browse', 'stats.read', 'audit.read', 'users.admin', 'settings.admin'],
}

export function testSession(capabilities: string[], username = 'nina'): Session {
  return {
    user: { id: `id-${username}`, username, display_name: username, email: null, source: 'local' },
    csrf_token: 'test-csrf',
    capabilities,
    preferences: { theme: 'system', density: 'comfortable' },
    expires_at: new Date(Date.now() + 3_600_000).toISOString(),
    must_change_password: false,
    totp_setup_required: false,
  }
}

/** Sign the (active Pinia's) auth store in with these capabilities, without a request. */
export function signInAs(capabilities: string[], username?: string): void {
  const auth = useAuthStore()
  auth.session = testSession(capabilities, username)
  auth.checked = true
}
