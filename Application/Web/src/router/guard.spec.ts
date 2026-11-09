// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'
import type { RouteLocationNormalized } from 'vue-router'

import { authGuard, safeNext } from './guard'

function route(partial: Partial<RouteLocationNormalized>): RouteLocationNormalized {
  return { fullPath: '/', path: '/', name: undefined, query: {}, params: {}, hash: '', matched: [], meta: {}, redirectedFrom: undefined, ...partial } as RouteLocationNormalized
}

const auth = (signedIn: boolean) => ({ signedIn, ensureChecked: async () => {} })

describe('authGuard', () => {
  it('sends anonymous users to sign-in, remembering where they were', async () => {
    expect(await authGuard(route({ fullPath: '/repositories?format=npm' }), auth(false))).toEqual({ name: 'sign-in', query: { next: '/repositories?format=npm' } })
    expect(await authGuard(route({ fullPath: '/' }), auth(false))).toEqual({ name: 'sign-in', query: {} })
  })

  it('lets signed-in users through', async () => {
    expect(await authGuard(route({ fullPath: '/repositories' }), auth(true))).toBe(true)
  })

  it('bounces signed-in users away from the sign-in page', async () => {
    const signIn = route({ name: 'sign-in', meta: { public: true }, query: { next: '/browse' } })
    expect(await authGuard(signIn, auth(true))).toBe('/browse')
    expect(await authGuard(signIn, auth(false))).toBe(true)
  })

  it('keeps a user with a one-time password on the profile page', async () => {
    const pending = { signedIn: true, mustChangePassword: true, ensureChecked: async () => {} }
    expect(await authGuard(route({ fullPath: '/repositories', name: 'repositories' }), pending)).toEqual({ name: 'profile' })
    expect(await authGuard(route({ fullPath: '/profile', name: 'profile' }), pending)).toBe(true)
  })

  it('keeps a user who must set up two-factor sign-in on the profile page', async () => {
    const pending = { signedIn: true, totpSetupRequired: true, ensureChecked: async () => {} }
    expect(await authGuard(route({ fullPath: '/users', name: 'admin-users' }), pending)).toEqual({ name: 'profile' })
    expect(await authGuard(route({ fullPath: '/profile', name: 'profile' }), pending)).toBe(true)
  })

  it('refuses open redirects', () => {
    expect(safeNext('//evil.example.com')).toBe('/')
    expect(safeNext('https://evil.example.com')).toBe('/')
    expect(safeNext('/ok')).toBe('/ok')
  })
})
