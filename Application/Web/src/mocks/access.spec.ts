// SPDX-License-Identifier: GPL-3.0-or-later
// Mock RBAC (mocks/access.ts) mirrors the server's answers for minimal-permission users (SPEC §4.10).
import { describe, expect, it } from 'vitest'

import { decide } from './access'
import { AdminWorld } from './admin'
import { CUSTOM_ROLES, MINIMAL_USERS } from './fixtures/admin'
import { ROLES, seedProviders } from './fixtures/authProviders'
import { seedRepositories } from './fixtures/repositories'

const repos = seedRepositories()
const roles = [...ROLES.filter((r) => r.builtin), ...CUSTOM_ROLES].map((r) => ({ ...r, permissions: [...r.permissions] }))
const providers = seedProviders()
const world = new AdminWorld({ roles, providers: () => providers, repos: () => all, serviceAccounts: () => [] })

const proxy = repos.find((r) => r.type === 'proxy')!
// The demo world mirrors public proxies only; a hosted repository shows what the anonymous grants hide.
const hosted = { ...proxy, name: 'releases-hosted', type: 'hosted' as const, proxy: undefined }
const all = [...repos, hosted]

async function answer(user: string, method: string, path: string): Promise<{ status: number; code?: string } | null> {
  const response = decide(world, user, method, path, all)
  if (!response) return null
  const body = (await response.json()) as { code?: string }
  return { status: response.status, code: body.code }
}

describe('mock access rules', () => {
  it('refuses statistics, activity and administration to a user without roles with 403 permission_required', async () => {
    const nobody = MINIMAL_USERS.noRoles
    for (const path of ['/stats/summary', '/stats/traffic', '/stats/storage', '/stats/upstreams', '/activity', '/transfers', '/tasks/scheduled', '/users', '/audit', '/settings', '/system', '/cleanup-policies']) {
      expect(await answer(nobody, 'GET', path), path).toEqual({ status: 403, code: 'permission_required' })
    }
  })

  it('lets a user without roles use what any signed-in user may', async () => {
    for (const path of ['/auth/me', '/auth/me/tokens', '/roles', '/repositories', '/search', '/storage/guard']) {
      expect(await answer(MINIMAL_USERS.noRoles, 'GET', path), path).toBeNull()
    }
  })

  it('gives a user without roles what anonymous access allows: signing in never reduces access', async () => {
    const nina = MINIMAL_USERS.noRoles
    expect(world.capabilities(nina)).toEqual(['repositories.browse'])
    expect(await answer(nina, 'GET', `/repositories/${proxy.name}/browse`)).toBeNull()
    expect(await answer(nina, 'PUT', `/repositories/${proxy.name}`)).toEqual({ status: 403, code: 'permission_required' })
    expect(await answer(nina, 'GET', `/repositories/${hosted.name}`)).toMatchObject({ status: 404 })
    const effective = world.userPermissions('01JC0000000000000000000U10')
    expect(effective.roles).toEqual([{ id: 'anonymous', name: 'Anonymous', via: ['anonymous'] }])
    expect(effective.repositories.map((r) => r.name)).not.toContain(hosted.name)
  })

  it('hides every repository from a user without roles when anonymous access is off (404, never 403)', async () => {
    const before = [...world.anonymousRoles]
    world.setAnonymous({ roles: [] })
    try {
      expect(world.capabilities(MINIMAL_USERS.noRoles)).toEqual([])
      for (const repo of [proxy, hosted]) {
        expect(await answer(MINIMAL_USERS.noRoles, 'GET', `/repositories/${repo.name}`)).toMatchObject({ status: 404 })
        expect(await answer(MINIMAL_USERS.noRoles, 'PUT', `/repositories/${repo.name}`)).toMatchObject({ status: 404 })
      }
    } finally {
      world.setAnonymous({ roles: before })
    }
  })

  it('shows the anonymous-access grants only proxies, read-only', async () => {
    const gus = MINIMAL_USERS.proxyOnly
    expect(await answer(gus, 'GET', `/repositories/${proxy.name}/browse`)).toBeNull()
    expect(await answer(gus, 'PUT', `/repositories/${proxy.name}`)).toEqual({ status: 403, code: 'permission_required' })
    expect(await answer(gus, 'GET', `/repositories/${hosted.name}`)).toMatchObject({ status: 404 })
  })

  it('gives a reader every repository but no statistics or cleanup', async () => {
    const reed = MINIMAL_USERS.reader
    expect(await answer(reed, 'GET', `/repositories/${hosted.name}`)).toBeNull()
    expect(await answer(reed, 'GET', `/repositories/${hosted.name}/cleanup`)).toMatchObject({ status: 403 })
    expect(await answer(reed, 'GET', '/stats/summary')).toMatchObject({ status: 403 })
    expect(await answer(reed, 'GET', '/cleanup-policies')).toMatchObject({ status: 403 })
  })

  it('lets an administrator through everywhere', async () => {
    for (const path of ['/stats/summary', '/tasks/scheduled', '/users', `/repositories/${hosted.name}/cleanup`]) {
      expect(await answer('admin', 'GET', path), path).toBeNull()
    }
  })
})
