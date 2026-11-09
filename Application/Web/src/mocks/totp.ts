// SPDX-License-Identifier: GPL-3.0-or-later
// Mock two-factor sign-in (T-O08): the second sign-in step, the profile card's enrolment / recovery codes / turn off.
// Accounts with it on (seeded: `sam`; or after setting it up) are asked for a code after the password: any 6 digits
// work except 000000 (the error state), as does any recovery code shown by the mock. State lives on the mock users
// (`totp_enabled`) and here (recovery codes left), and is lost on reload like the rest of the mock world.
import { delay, http, HttpResponse } from 'msw'

import type { Problem, Session, TotpSetup } from '@/api/types'

import type { AdminWorld } from './admin'
import type { MockHelpers } from './tokens'

const API = '/api/v1'
const PENDING_KEY = 'ws.mock.mfa'
// RFC 4648 test-vector style key: obviously not a real secret.
const MOCK_SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'
const BAD_CODE = '000000'

function problemBody(status: number, title: string, detail: string, code?: string, field?: string): Response {
  const body: Problem = {
    type: 'about:blank',
    title,
    status,
    detail,
    ...(code ? { code } : {}),
    ...(field ? { errors: [{ field, message: detail, code: 'code' }] } : {}),
  }
  return HttpResponse.json(body, { status, headers: { 'Content-Type': 'application/problem+json' } })
}

function recoveryCodes(): string[] {
  const alphabet = '23456789abcdefghjkmnpqrstuvwxyz'
  return Array.from({ length: 10 }, () => {
    const raw = Array.from({ length: 10 }, () => alphabet[Math.floor(Math.random() * alphabet.length)]).join('')
    return `${raw.slice(0, 5)}-${raw.slice(5)}`
  })
}

export interface TotpMockDeps extends MockHelpers {
  /** The session the mock would return for this username (after a successful second step). */
  session: (username: string) => Session
  /** Mark the browser signed in as `username`. */
  signIn: (username: string) => void
}

export class TotpState {
  /** user id -> recovery codes still unused (only the ones this mock issued; seeded users get a count). */
  private readonly recovery = new Map<string, string[]>()
  private readonly enabledAt = new Map<string, string>()
  private pendingSetup: string | null = null
  private readonly admin: AdminWorld

  constructor(admin: AdminWorld) {
    this.admin = admin
  }

  private record(userId: string) {
    return this.admin.users.find((u) => u.id === userId)
  }

  enabled(userId: string): boolean {
    return Boolean(this.record(userId)?.totp_enabled)
  }

  /** Does policy keep this signed-in user on the profile page until it sets up two-factor sign-in? */
  setupRequired(username: string, userId: string): boolean {
    const user = this.record(userId)
    return this.admin.requireTotpForAdmins && user?.source === 'local' && !user.totp_enabled && this.admin.callerGrants(username).admin
  }

  /** Begin a pending sign-in when the account has two-factor sign-in on. */
  startSignIn(username: string, userId: string): boolean {
    if (!this.enabled(userId)) return false
    localStorage.setItem(PENDING_KEY, username)
    return true
  }

  status(userId: string) {
    const user = this.record(userId)
    const on = Boolean(user?.totp_enabled)
    return {
      available: user ? user.source === 'local' && user.has_password : true,
      enabled: on,
      enabled_at: on ? (this.enabledAt.get(userId) ?? new Date(Date.now() - 40 * 86_400_000).toISOString()) : null,
      required: false,
      recovery_codes_left: on ? (this.recovery.get(userId)?.length ?? 8) : 0,
    }
  }

  setEnabled(userId: string, on: boolean, codes: string[] = []): void {
    const user = this.record(userId)
    if (user) user.totp_enabled = on
    if (on) {
      this.enabledAt.set(userId, new Date().toISOString())
      this.recovery.set(userId, codes)
    } else {
      this.enabledAt.delete(userId)
      this.recovery.delete(userId)
    }
  }

  useCode(userId: string, code: string, allowRecovery = true): boolean {
    const value = code.trim().toLowerCase()
    if (/^\d{6}$/.test(value)) return value !== BAD_CODE
    if (!allowRecovery) return false
    const left = this.recovery.get(userId)
    const normal = value.replace(/[\s-]/g, '')
    if (!left) return /^[a-z0-9]{10}$/.test(normal) // seeded users: any well-formed recovery code
    const index = left.findIndex((c) => c.replace('-', '') === normal)
    if (index < 0) return false
    left.splice(index, 1)
    return true
  }

  handlers(deps: TotpMockDeps) {
    const me = () => {
      const name = deps.currentUser()
      return name ? { name, id: deps.session(name).user.id } : null
    }
    return [
      http.post(`${API}/auth/login/totp`, async ({ request }) => {
        await delay(300)
        const username = localStorage.getItem(PENDING_KEY)
        if (!username) return problemBody(401, 'Unauthorized', 'the sign-in expired; enter your password again', 'second_factor_expired')
        const { code } = (await request.json()) as { code: string }
        if (!this.useCode(deps.session(username).user.id, code)) return problemBody(401, 'Unauthorized', 'invalid code')
        localStorage.removeItem(PENDING_KEY)
        deps.signIn(username)
        return HttpResponse.json(deps.session(username))
      }),
      http.get(`${API}/auth/me/totp`, async ({ request }) => {
        const denied = deps.guard(request)
        if (denied) return denied
        const user = me()!
        return HttpResponse.json({ ...this.status(user.id), required: this.setupRequired(user.name, user.id) })
      }),
      http.post(`${API}/auth/me/totp/setup`, async ({ request }) => {
        const denied = deps.guard(request)
        if (denied) return denied
        await deps.latency()
        const user = me()!
        const { password } = (await request.json()) as { password: string }
        if (password === 'wrong') return problemBody(422, 'Unprocessable Content', 'not your current password', undefined, 'password')
        if (this.enabled(user.id)) return deps.problem(409, 'Conflict', 'two-factor sign-in is already on')
        this.pendingSetup = user.id
        const issuer = this.admin.instanceName.replace(/:/g, ' ')
        const body: TotpSetup = {
          secret: MOCK_SECRET,
          otpauth_uri: `otpauth://totp/${encodeURIComponent(issuer)}:${encodeURIComponent(user.name)}?secret=${MOCK_SECRET}&issuer=${encodeURIComponent(issuer)}&algorithm=SHA1&digits=6&period=30`,
          issuer,
          account: user.name,
          expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
        }
        return HttpResponse.json(body)
      }),
      http.post(`${API}/auth/me/totp/confirm`, async ({ request }) => {
        const denied = deps.guard(request)
        if (denied) return denied
        await deps.latency()
        const user = me()!
        if (this.pendingSetup !== user.id) return deps.problem(409, 'Conflict', 'set-up expired or was not started; start again')
        const { code } = (await request.json()) as { code: string }
        if (!this.useCode(user.id, code, false)) return problemBody(422, 'Unprocessable Content', 'the code is not valid (or was already used)', undefined, 'code')
        const codes = recoveryCodes()
        this.pendingSetup = null
        this.setEnabled(user.id, true, [...codes])
        return HttpResponse.json({ recovery_codes: codes })
      }),
      http.post(`${API}/auth/me/totp/recovery-codes`, async ({ request }) => {
        const denied = deps.guard(request)
        if (denied) return denied
        await deps.latency()
        const user = me()!
        if (!this.enabled(user.id)) return deps.problem(409, 'Conflict', 'two-factor sign-in is off')
        const { code } = (await request.json()) as { code: string }
        if (!this.useCode(user.id, code, false)) return problemBody(422, 'Unprocessable Content', 'the code is not valid (or was already used)', undefined, 'code')
        const codes = recoveryCodes()
        this.recovery.set(user.id, [...codes])
        return HttpResponse.json({ recovery_codes: codes })
      }),
      http.post(`${API}/auth/me/totp/disable`, async ({ request }) => {
        const denied = deps.guard(request)
        if (denied) return denied
        await deps.latency()
        const user = me()!
        if (!this.enabled(user.id)) return deps.problem(409, 'Conflict', 'two-factor sign-in is off')
        const body = (await request.json()) as { password: string; code: string }
        if (body.password === 'wrong') return problemBody(422, 'Unprocessable Content', 'not your current password', undefined, 'password')
        if (!this.useCode(user.id, body.code)) return problemBody(422, 'Unprocessable Content', 'the code is not valid (or was already used)', undefined, 'code')
        this.setEnabled(user.id, false)
        return new HttpResponse(null, { status: 204 })
      }),
    ]
  }
}
