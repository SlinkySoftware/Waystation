// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { AdminWorld, MockProblem } from './admin'
import { ADMIN_USER_ID, CUSTOM_ROLES } from './fixtures/admin'
import { ROLES, seedProviders } from './fixtures/authProviders'
import { seedRepositories } from './fixtures/repositories'

function world(): AdminWorld {
  const roles = [...ROLES.filter((r) => r.builtin), ...CUSTOM_ROLES].map((r) => ({ ...r, permissions: [...r.permissions] }))
  const providers = seedProviders()
  const repos = seedRepositories()
  return new AdminWorld({ roles, providers: () => providers, repos: () => repos, serviceAccounts: () => [{ id: 'sa1', name: 'ci', description: '', roles: ['npm-publisher'], disabled: false, active_tokens: 0, created_at: '2026-11-01T00:00:00Z' }] })
}

function problem(fn: () => unknown): MockProblem {
  try {
    fn()
  } catch (error) {
    if (error instanceof MockProblem) return error
    throw error
  }
  throw new Error('expected a problem')
}

describe('mock users', () => {
  it('pages by username with a cursor and filters by source, status and role', () => {
    const w = world()
    const first = w.listUsers(new URLSearchParams({ limit: '20' }))
    expect(first.items).toHaveLength(20)
    expect(first.items.map((u) => u.username)).toEqual([...first.items.map((u) => u.username)].sort())
    const second = w.listUsers(new URLSearchParams({ limit: '20', cursor: first.next_cursor! }))
    expect(second.items[0]!.username > first.items.at(-1)!.username).toBe(true)
    expect(w.listUsers(new URLSearchParams({ source: 'ldap', limit: '200' })).items.every((u) => u.source.startsWith('ldap:'))).toBe(true)
    expect(w.listUsers(new URLSearchParams({ disabled: 'true' })).items.map((u) => u.username)).toEqual(['erin'])
    expect(w.listUsers(new URLSearchParams({ q: 'ali' })).items.map((u) => u.username)).toEqual(['alice'])
  })

  it('resolves roles through local and mapped groups', () => {
    const w = world()
    const alice = w.detail(w.user('01JC0000000000000000000U01'))
    expect(alice.effective_roles.sort()).toEqual(['deployer', 'reader', 'viewer'])
    expect(alice.group_refs.every((g) => g.source === 'ldap')).toBe(true)
    const perms = w.userPermissions(alice.id)
    expect(perms.global_actions).toEqual(['stats-read'])
    expect(perms.repositories.every((r) => r.actions.includes('write'))).toBe(true)
  })

  it('refuses disabling yourself and removing the last administrator (409)', () => {
    const w = world()
    expect(problem(() => w.updateUser('admin', ADMIN_USER_ID, { disabled: true })).status).toBe(409)
    expect(problem(() => w.updateUser('helpdesk', ADMIN_USER_ID, { roles: [] })).status).toBe(403) // holds more
    const demote = problem(() => w.updateUser('someone-else', ADMIN_USER_ID, { roles: ['reader'] }))
    expect(demote.status).toBe(409)
    expect(demote.detail).toContain('no enabled administrator')
    expect(w.user(ADMIN_USER_ID).roles).toEqual(['admin']) // undone
    expect(problem(() => w.deleteUser('admin', ADMIN_USER_ID)).status).toBe(409)
  })

  it('bounds delegation by what the caller holds (403 / 422)', () => {
    const w = world()
    const refused = problem(() => w.createUser('helpdesk', { display_name: '', must_change_password: true, username: 'newbie', roles: ['admin'] }))
    expect(refused.status).toBe(422)
    expect(refused.errors?.[0]?.field).toBe('roles.0')
    expect(w.createUser('helpdesk', { display_name: '', must_change_password: true, username: 'newbie', roles: ['reader'] }).roles).toEqual(['reader'])
    expect(w.capabilities('helpdesk')).toEqual(['repositories.browse', 'users.admin'])
    expect(w.capabilities('nina')).toEqual(['repositories.browse']) // no role: what anonymous access allows
    expect(w.capabilities('reed')).toEqual(['repositories.browse'])
    expect(w.capabilities('gus')).toEqual(['repositories.browse'])
    expect(w.capabilities('admin')).toContain('admin')
  })

  it('creates, resets, unlocks and signs out', () => {
    const w = world()
    const created = w.createUser('admin', { display_name: '', must_change_password: true, username: 'Jordan', password: 'x'.repeat(12) })
    expect(created).toMatchObject({ username: 'jordan', must_change_password: true, has_password: true })
    expect(problem(() => w.createUser('admin', { display_name: '', must_change_password: true, username: 'jordan' })).status).toBe(409)
    expect(problem(() => w.resetPassword('admin', '01JC0000000000000000000U01', { password: 'x'.repeat(12), must_change_password: true })).status).toBe(409) // LDAP
    expect(w.unlock('admin', '01JC0000000000000000000U05').locked).toBe(false)
    expect(w.revokeSessions('admin', '01JC0000000000000000000U01')).toBeGreaterThan(0)
    expect(w.userSessions('01JC0000000000000000000U01', 'admin')).toEqual([])
  })
})

describe('mock groups and roles', () => {
  it('lists local then mapped groups; mapped groups take roles only', () => {
    const w = world()
    const groups = w.allGroups()
    expect(groups[0]!.source).toBe('local')
    const mapped = groups.find((g) => g.source === 'ldap' && g.external_group === 'ops')!
    expect(problem(() => w.updateGroup('admin', mapped.id, { name: 'x' })).status).toBe(409)
    expect(w.updateGroup('admin', mapped.id, { roles: ['viewer'] }).roles).toEqual(['viewer'])
    expect(problem(() => w.deleteGroup('admin', mapped.id)).status).toBe(409)
  })

  it('manages local group members', () => {
    const w = world()
    const group = w.createGroup('admin', { name: 'Ops', description: '', roles: ['viewer'] })
    w.addMembers('admin', group.id, { user_ids: ['01JC0000000000000000000U03'] })
    expect(w.members(group.id, new URLSearchParams()).items.map((u) => u.username)).toEqual(['dana'])
    w.removeMember('admin', group.id, '01JC0000000000000000000U03')
    expect(w.group(group.id).member_count).toBe(0)
  })

  it('keeps built-in roles read-only and roles in use undeletable', () => {
    const w = world()
    expect(problem(() => w.updateRole('admin', 'reader', { name: 'R', description: '', permissions: [{ action: 'read', scope: '*' }] })).status).toBe(409)
    expect(problem(() => w.deleteRole('admin', 'npm-publisher')).detail).toContain('still held by')
    expect(w.roleDetail('npm-publisher').usage).toEqual({ users: 0, groups: 1, service_accounts: 1 })
    const made = w.createRole('admin', { id: 'docs', name: 'Docs', description: '', permissions: [{ action: 'read', scope: 'raw-*' }] })
    expect(made.builtin).toBe(false)
    expect(problem(() => w.createRole('helpdesk', { id: 'auditor2', name: 'A', description: '', permissions: [{ action: 'audit-read', scope: '*' }] })).errors?.[0]?.code).toBe('delegation')
    expect(problem(() => w.createRole('admin', { id: 'bad', name: 'B', description: '', permissions: [{ action: 'admin', scope: 'npm' }] })).status).toBe(422)
    w.deleteRole('admin', 'docs')
    expect(w.role('docs')).toBeUndefined()
  })

  it('anonymous access takes repository-only roles', () => {
    const w = world()
    expect(w.setAnonymous({ roles: ['anonymous', 'docker-mirror'] }).roles).toEqual(['anonymous', 'docker-mirror'])
    expect(problem(() => w.setAnonymous({ roles: ['viewer'] })).status).toBe(422)
  })
})

describe('mock settings and system', () => {
  it('saves the instance name and reports the system', () => {
    const w = world()
    expect(problem(() => w.saveSettings('admin', { general: { instance_name: '  ' } })).status).toBe(422)
    const saved = w.saveSettings('admin', { general: { instance_name: ' Mirror ' } })
    expect(saved.general.instance_name).toBe('Mirror')
    expect(saved.updated_by).toBe('admin')
    const system = w.system({ total: 100, used: 40 })
    expect(system.components.map((c) => c.status)).toEqual(['ok', 'ok'])
    expect(system.storage.free_bytes).toBe(60)
  })
})
