// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { UserDetail } from '@/api/types'

import {
  emptyUserModel,
  generatePassword,
  matrixProblems,
  mayAssignRole,
  modelFromUser,
  passwordProblem,
  slugify,
  toAccountUpdate,
  toggleAction,
  toGroupUpdate,
  toRoleCreate,
  toUserCreate,
  toUserUpdate,
} from './forms'

const user = (over: Partial<UserDetail> = {}): UserDetail => ({
  id: 'u1',
  username: 'dana',
  display_name: 'Dana',
  email: null,
  source: 'local',
  disabled: false,
  locked: false,
  totp_enabled: false,
  roles: ['reader'],
  groups: ['g1'],
  last_login: null,
  created_at: '2026-11-01T00:00:00Z',
  locked_until: null,
  must_change_password: false,
  has_password: true,
  group_refs: [],
  external_groups: [],
  identities: [],
  effective_roles: [],
  ...over,
})

describe('delegation hints', () => {
  const caps = (...held: string[]) => (c: string) => held.includes('admin') || held.includes(c)
  it('lets administrators grant anything and others only what they hold, never admin', () => {
    const viewerish = { permissions: [{ action: 'stats-read' as const, scope: '*' }] }
    const adminRole = { permissions: [{ action: 'admin' as const, scope: '*' }] }
    const repoOnly = { permissions: [{ action: 'write' as const, scope: 'npm' }] }
    expect(mayAssignRole(adminRole, caps('admin'))).toBe(true)
    expect(mayAssignRole(adminRole, caps('users.admin', 'stats.read'))).toBe(false)
    expect(mayAssignRole(viewerish, caps('users.admin', 'stats.read'))).toBe(true)
    expect(mayAssignRole(viewerish, caps('users.admin'))).toBe(false)
    expect(mayAssignRole(repoOnly, caps('users.admin'))).toBe(true)
  })
})

describe('user form', () => {
  it('creates a local user, with or without a password', () => {
    const m = { ...emptyUserModel(), username: ' Jordan ', display_name: ' Jordan E ', email: '', password: 'p'.repeat(12), roles: ['viewer'] }
    expect(toUserCreate(m)).toEqual({ username: 'jordan', display_name: 'Jordan E', email: null, password: 'p'.repeat(12), must_change_password: true, roles: ['viewer'], groups: [] })
    expect(toUserCreate({ ...m, withPassword: false })).toMatchObject({ password: null, must_change_password: false })
  })

  it('sends only what changed, and no profile for provider accounts', () => {
    const local = user()
    expect(toUserUpdate(modelFromUser(local), local)).toEqual({})
    expect(toUserUpdate({ ...modelFromUser(local), email: 'dana@example.com', roles: ['reader', 'viewer'] }, local)).toEqual({ email: 'dana@example.com', roles: ['reader', 'viewer'] })
    expect(toUserUpdate({ ...modelFromUser(local), groups: ['g1'], roles: ['reader'] }, local)).toEqual({})
    const ldap = user({ source: 'ldap:01P' })
    expect(toUserUpdate({ ...modelFromUser(ldap), display_name: 'Other', groups: [] }, ldap)).toEqual({ groups: [] })
  })

  it('checks and generates passwords', () => {
    expect(passwordProblem('short', 'x')).toBe('short')
    expect(passwordProblem('Dana.example', 'dana.example')).toBe('username')
    expect(passwordProblem('a long enough one', 'dana')).toBeNull()
    const pw = generatePassword(4, (n) => Uint32Array.from({ length: n }, (_, i) => i * 7))
    expect(pw).toMatch(/^[A-Za-z2-9]{4}(-[A-Za-z2-9]{4}){3}$/)
    expect(pw).not.toMatch(/[0O1lI]/)
    expect(passwordProblem(generatePassword(), 'dana')).toBeNull()
  })
})

describe('group and service account forms', () => {
  it('mapped groups only send roles', () => {
    const mapped = { name: 'ldap:Dir:ops', description: '', roles: ['viewer'], source: 'ldap' }
    expect(toGroupUpdate({ name: 'renamed', description: 'x', roles: ['viewer', 'reader'] }, mapped)).toEqual({ roles: ['viewer', 'reader'] })
    const local = { name: 'Ops', description: '', roles: [], source: 'local' }
    expect(toGroupUpdate({ name: ' Ops team ', description: '', roles: [] }, local)).toEqual({ name: 'Ops team' })
  })

  it('service account updates carry changes only', () => {
    const original = { description: 'CI', roles: ['deployer'], disabled: false }
    expect(toAccountUpdate({ name: 'ci', description: 'CI', roles: ['deployer'], disabled: true }, original)).toEqual({ disabled: true })
  })
})

describe('role form', () => {
  it('suggests ids from names', () => {
    expect(slugify('npm Publishers (EU)')).toBe('npm-publishers-eu')
    expect(slugify('2nd line')).toBe('nd-line')
  })

  it('toggles actions with their implications', () => {
    expect(toggleAction([], 'delete', true)).toEqual(['browse', 'read', 'delete'])
    expect(toggleAction(['browse', 'read', 'write'], 'read', false)).toEqual(['browse'])
    expect(toggleAction(['browse', 'read', 'write'], 'write', false)).toEqual(['browse', 'read'])
  })

  it('reports row problems and an empty role', () => {
    const p = matrixProblems({ rows: [{ scope: 'npm', actions: ['read'] }, { scope: 'NPM', actions: ['read'] }, { scope: 'bad scope', actions: ['read'] }, { scope: 'raw', actions: [] }], global: [] })
    expect(p.rows).toEqual({ 1: 'duplicate', 2: 'scope', 3: 'actions' })
    expect(matrixProblems({ rows: [], global: [] }).empty).toBe(true)
    expect(matrixProblems({ rows: [], global: ['audit-read'] }).empty).toBe(false)
  })

  it('creates a role from the matrix', () => {
    expect(toRoleCreate({ id: 'docs', name: ' Docs ', description: '', matrix: { rows: [{ scope: 'raw-*', actions: ['browse', 'read'] }], global: ['stats-read'] } })).toEqual({
      id: 'docs',
      name: 'Docs',
      description: '',
      permissions: [
        { action: 'browse', scope: 'raw-*' },
        { action: 'read', scope: 'raw-*' },
        { action: 'stats-read', scope: '*' },
      ],
    })
  })
})
