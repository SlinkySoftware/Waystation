// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { fromConfig, toWrite } from '@/components/auth/providerForm'

import { seedProviders, testConnection, testUser, toConfig, validateProvider } from './fixtures/authProviders'

const seeded = seedProviders()[0]!
const body = () => toWrite(fromConfig(seeded))

describe('mock sign-in providers', () => {
  it('seeds one FreeIPA-style directory on example.com only', () => {
    expect(seeded.name).toBe('Corporate directory')
    expect(seeded.ldap!.connection.bind_password_set).toBe(true)
    for (const s of seeded.ldap!.connection.servers) expect(s).toMatch(/\.example\.com$/)
    expect(seeded.ldap!.users.base_dn).toContain('dc=example,dc=com')
  })

  it('demonstrates failover: the second server does not answer', () => {
    const result = testConnection(body(), seeded)
    expect(result.ok).toBe(false)
    expect(result.servers.map((s) => s.ok)).toEqual([true, false])
    expect(result.servers[0]!.checks.map((c) => c.name)).toEqual(['connect', 'tls', 'bind', 'user_base', 'group_base'])
    expect(result.servers[1]!.latency_ms).toBeNull()
  })

  it('needs a bind password for a new provider with a bind DN', () => {
    const write = body()
    const result = testConnection(write)
    expect(result.servers[0]!.checks.find((c) => c.name === 'bind')?.ok).toBe(false)
  })

  it('looks users up with groups, the require-group gate and mapped roles', () => {
    const alice = testUser(body(), 'Alice', 'secret')
    expect(alice).toMatchObject({ ok: true, found: true, password: 'ok', require_group_ok: true, roles: ['deployer'] })
    expect(alice.groups.map((g) => g.name)).toEqual(['developers', 'waystation-users'])
    expect(alice.user?.dn).toBe('uid=alice,cn=users,cn=accounts,dc=example,dc=com')

    expect(testUser(body(), 'alice', 'wrong')).toMatchObject({ ok: false, password: 'failed' })
    expect(testUser(body(), 'alice', null).password).toBe('not_tested')
    expect(testUser(body(), 'dave', null)).toMatchObject({ ok: false, require_group_ok: false })
    expect(testUser(body(), 'mallory', 'x')).toMatchObject({ ok: false, found: false, user: null })
  })

  it('follows nested groups only when asked', () => {
    const write = body()
    expect(testUser(write, 'bob', null).groups.map((g) => g.name)).not.toContain('platform')
    write.ldap!.groups.nested = true
    expect(testUser(write, 'bob', null).groups.map((g) => g.name)).toContain('platform')
  })

  it('validates with field paths into the request body', () => {
    const write = body()
    write.name = 'corporate DIRECTORY'
    write.ldap!.connection.servers = ['ldaps://ok.example.com', 'not a host']
    write.ldap!.users.filter = 'objectClass=person'
    write.group_mapping = [{ external_group: 'x', roles: ['wizard'] }]
    const fields = validateProvider(write, [seeded]).map((e) => e.field)
    expect(fields).toEqual(['name', 'ldap.connection.servers.1', 'ldap.users.filter', 'group_mapping.0.roles'])
  })

  it('seeds a Keycloak OIDC provider and tests its discovery', () => {
    const oidc = seedProviders().find((p) => p.kind === 'oidc')!
    expect(oidc.oidc!.issuer).toMatch(/^https:\/\/[a-z.]+\.example\.com\//)
    const write = toWrite(fromConfig(oidc))
    const result = testConnection(write, oidc)
    expect(result.ok).toBe(true)
    expect(result.discovery?.keys).toBeGreaterThan(0)
    expect(result.servers[0]!.checks.map((c) => c.name)).toContain('client')
    // a new provider without a secret fails the client check
    expect(testConnection(write).servers[0]!.checks.find((c) => c.name === 'client')?.ok).toBe(false)
  })

  it('validates OIDC settings and stores the client secret as a flag only', () => {
    const write = toWrite(fromConfig(seedProviders().find((p) => p.kind === 'oidc')!))
    write.oidc!.issuer = 'http://sso.example.com/realms/example'
    write.oidc!.client_secret = null
    const fields = validateProvider(write, []).map((e) => e.field)
    expect(fields).toEqual(['oidc.issuer', 'oidc.client_secret'])
    write.oidc!.client_secret = 'new-secret'
    const config = toConfig(write)
    expect(config.oidc!.client_secret_set).toBe(true)
    expect(config.ldap).toBeNull()
    expect(JSON.stringify(config)).not.toContain('new-secret')
  })

  it('stores the password as a flag only', () => {
    const write = body()
    write.ldap!.connection.bind_password = 'new-password'
    const config = toConfig(write, seeded)
    expect(config.ldap!.connection.bind_password_set).toBe(true)
    expect(JSON.stringify(config)).not.toContain('new-password')
  })
})
