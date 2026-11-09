// SPDX-License-Identifier: GPL-3.0-or-later
// Auth guard: private routes need a session; the sign-in page bounces signed-in users onwards; a user holding a
// one-time password is kept on the profile page (where it is changed) until it is changed (the server refuses
// everything else for that session anyway, SPEC §4.9).
import type { RouteLocationNormalized, RouteLocationRaw } from 'vue-router'

export interface GuardAuth {
  ensureChecked(): Promise<void>
  readonly signedIn: boolean
  readonly mustChangePassword?: boolean
}

/** Only same-app paths are accepted as `next` (no open redirects). */
export function safeNext(value: unknown): string {
  return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//') ? value : '/'
}

export async function authGuard(to: RouteLocationNormalized, auth: GuardAuth): Promise<true | RouteLocationRaw> {
  await auth.ensureChecked()
  if (to.meta.public) {
    if (to.name === 'sign-in' && auth.signedIn) return safeNext(to.query.next)
    return true
  }
  if (!auth.signedIn) return { name: 'sign-in', query: to.fullPath === '/' ? {} : { next: to.fullPath } }
  if (auth.mustChangePassword && to.name !== 'profile') return { name: 'profile' }
  return true
}
