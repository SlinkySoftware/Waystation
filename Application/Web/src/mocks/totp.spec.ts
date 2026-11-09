// SPDX-License-Identifier: GPL-3.0-or-later
// Two-factor sign-in mocks (T-O08): the second sign-in step, enrolment, recovery codes, turning off, the
// administrator's reset and the "required for local administrators" policy, through the MSW handlers.
import { getResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { handlers } from './handlers'

const CSRF = 'mock-csrf-7f3a9c'

async function send(method: string, path: string, body?: unknown): Promise<{ status: number; body: unknown }> {
  const request = new Request(new URL(`/api/v1${path}`, location.origin), {
    method,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const response = (await getResponse(handlers, request))!
  return { status: response.status, body: response.status === 204 ? null : await response.json() }
}

beforeEach(() => localStorage.clear())

describe('mock two-factor sign-in', () => {
  it('asks sam for a code after the password, and refuses 000000', async () => {
    const first = await send('POST', '/auth/login', { username: 'sam', password: 'secret' })
    expect(first.status).toBe(202)
    expect(first.body).toMatchObject({ second_factor: 'totp' })
    expect((await send('GET', '/auth/me')).status).toBe(401)
    const wrong = await send('POST', '/auth/login/totp', { code: '000000' })
    expect(wrong.status).toBe(401)
    const done = await send('POST', '/auth/login/totp', { code: '123456' })
    expect(done.status).toBe(200)
    expect(done.body).toMatchObject({ user: { username: 'sam' }, totp_setup_required: false })
    const again = await send('POST', '/auth/login/totp', { code: '123456' })
    expect(again.body).toMatchObject({ code: 'second_factor_expired' })
  })

  it('enrols, issues recovery codes once and turns off with one of them', async () => {
    expect((await send('POST', '/auth/login', { username: 'dana', password: 'secret' })).status).toBe(200)
    expect((await send('GET', '/auth/me/totp')).body).toMatchObject({ available: true, enabled: false })
    expect((await send('POST', '/auth/me/totp/setup', { password: 'wrong' })).status).toBe(422)
    const setup = await send('POST', '/auth/me/totp/setup', { password: 'secret' })
    expect(setup.body).toMatchObject({ account: 'dana' })
    expect((setup.body as { otpauth_uri: string }).otpauth_uri).toMatch(/^otpauth:\/\/totp\//)
    expect((await send('POST', '/auth/me/totp/confirm', { code: '000000' })).status).toBe(422)
    const confirmed = await send('POST', '/auth/me/totp/confirm', { code: '654321' })
    const codes = (confirmed.body as { recovery_codes: string[] }).recovery_codes
    expect(codes).toHaveLength(10)
    expect((await send('GET', '/auth/me/totp')).body).toMatchObject({ enabled: true, recovery_codes_left: 10 })

    expect((await send('POST', '/auth/me/totp/disable', { password: 'secret', code: 'aaaaa-aaaaa' })).status).toBe(422)
    expect((await send('POST', '/auth/me/totp/disable', { password: 'secret', code: codes[0] })).status).toBe(204)
    expect((await send('GET', '/auth/me/totp')).body).toMatchObject({ enabled: false })
  })

  it('lets an administrator reset another user, never themself', async () => {
    expect((await send('POST', '/auth/login', { username: 'admin', password: 'secret' })).status).toBe(200)
    const sam = '01JC0000000000000000000U09'
    expect((await send('GET', `/users/${sam}`)).body).toMatchObject({ totp_enabled: true })
    expect((await send('DELETE', `/users/${sam}/totp`)).status).toBe(204)
    expect((await send('GET', `/users/${sam}`)).body).toMatchObject({ totp_enabled: false })
    expect((await send('DELETE', `/users/${sam}/totp`)).status).toBe(409)
  })

  it('keeps a local administrator without it on set-up while the policy is on', async () => {
    expect((await send('POST', '/auth/login', { username: 'admin', password: 'secret' })).status).toBe(200)
    const settings = (await send('GET', '/settings')).body as { general: { instance_name: string } }
    const saved = await send('PUT', '/settings', { general: settings.general, security: { require_totp_for_admins: true } })
    expect(saved.body).toMatchObject({ security: { require_totp_for_admins: true } })
    expect((await send('GET', '/auth/me')).body).toMatchObject({ totp_setup_required: true })
    expect((await send('GET', '/auth/me/totp')).body).toMatchObject({ required: true })
    await send('PUT', '/settings', { general: settings.general, security: { require_totp_for_admins: false } })
    expect((await send('GET', '/auth/me')).body).toMatchObject({ totp_setup_required: false })
  })
})
