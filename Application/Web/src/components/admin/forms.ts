// SPDX-License-Identifier: GPL-3.0-or-later
// Form models of the administration dialogs (users, groups, roles, service accounts) and what each sends: only the
// fields that changed, in the contract's shapes. Delegation hints mirror SPEC §4.10; the server decides.
import type { GroupCreate, GroupUpdate, Role, RoleCreate, RoleWrite, ServiceAccountCreate, ServiceAccountUpdate, UserCreate, UserDetail, UserUpdate } from '@/api/types'
import { fromMatrix, type GlobalAction, isGlobalAction, type PermissionMatrix, REPO_ACTIONS, type RepoAction, scopeProblem, toMatrix } from '@/utils/rbac'

export const MIN_PASSWORD = 12

/** The capability (session hint) standing for each global action. */
const CAPABILITY: Record<GlobalAction, string> = {
  admin: 'admin',
  'user-admin': 'users.admin',
  'audit-read': 'audit.read',
  'stats-read': 'stats.read',
  'settings-admin': 'settings.admin',
}

/** Whether the caller may hand out `role` (global actions only when held, `admin` only by administrators). A hint for
 * disabling options: the server refuses anything else with 403/422. */
export function mayAssignRole(role: Pick<Role, 'permissions'>, can: (capability: string) => boolean): boolean {
  if (can('admin')) return true
  return role.permissions.every((p) => !isGlobalAction(p.action) || (p.action !== 'admin' && can(CAPABILITY[p.action])))
}

/** Whether `role` carries a global action (it cannot be given to anonymous access). */
export function hasGlobalAction(role: Pick<Role, 'permissions'>): boolean {
  return role.permissions.some((p) => isGlobalAction(p.action))
}

/** Client-side password rules (the server applies the same and more). */
export function passwordProblem(password: string, username: string): 'short' | 'username' | null {
  if (password.length < MIN_PASSWORD) return 'short'
  if (password.toLowerCase() === username.trim().toLowerCase()) return 'username'
  return null
}

const ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'

/** A random password from an unambiguous alphabet, grouped for reading aloud (`xxxx-xxxx-xxxx-xxxx`). */
export function generatePassword(groups = 4, random: (n: number) => Uint32Array = (n) => crypto.getRandomValues(new Uint32Array(n))): string {
  const values = random(groups * 4)
  const chars = Array.from(values, (v) => ALPHABET[v % ALPHABET.length])
  return Array.from({ length: groups }, (_, i) => chars.slice(i * 4, i * 4 + 4).join('')).join('-')
}

const sameSet = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((x) => b.includes(x))

// ---------------------------------------------------------------- users
export interface UserFormModel {
  username: string
  display_name: string
  email: string
  /** Create: set a password now (else the account waits for an LDAP/OIDC sign-in of the same username). */
  withPassword: boolean
  password: string
  mustChange: boolean
  roles: string[]
  groups: string[]
}

export function emptyUserModel(): UserFormModel {
  return { username: '', display_name: '', email: '', withPassword: true, password: '', mustChange: true, roles: [], groups: [] }
}

export function modelFromUser(user: UserDetail): UserFormModel {
  return { ...emptyUserModel(), username: user.username, display_name: user.display_name, email: user.email ?? '', withPassword: false, roles: [...user.roles], groups: [...user.groups] }
}

export function toUserCreate(m: UserFormModel): UserCreate {
  return {
    username: m.username.trim().toLowerCase(),
    display_name: m.display_name.trim(),
    email: m.email.trim() || null,
    password: m.withPassword ? m.password : null,
    must_change_password: m.withPassword && m.mustChange,
    roles: [...m.roles],
    groups: [...m.groups],
  }
}

/** Only what changed. Providers own the profile of external accounts, so it is sent for local accounts only. */
export function toUserUpdate(m: UserFormModel, original: UserDetail): UserUpdate {
  const out: UserUpdate = {}
  if (original.source === 'local') {
    const name = m.display_name.trim()
    if (name && name !== original.display_name) out.display_name = name
    const email = m.email.trim() || null
    if (email !== (original.email ?? null)) out.email = email
  }
  if (!sameSet(m.roles, original.roles)) out.roles = [...m.roles]
  if (!sameSet(m.groups, original.groups)) out.groups = [...m.groups]
  return out
}

// ---------------------------------------------------------------- groups
export interface GroupFormModel {
  name: string
  description: string
  roles: string[]
}

export function toGroupCreate(m: GroupFormModel): GroupCreate {
  return { name: m.name.trim(), description: m.description.trim(), roles: [...m.roles] }
}

/** Mapped groups only take role changes. */
export function toGroupUpdate(m: GroupFormModel, original: { name: string; description: string; roles: string[]; source: string }): GroupUpdate {
  const out: GroupUpdate = {}
  if (original.source === 'local') {
    if (m.name.trim() !== original.name) out.name = m.name.trim()
    if (m.description.trim() !== original.description) out.description = m.description.trim()
  }
  if (!sameSet(m.roles, original.roles)) out.roles = [...m.roles]
  return out
}

// ---------------------------------------------------------------- roles
export interface RoleFormModel {
  id: string
  name: string
  description: string
  matrix: PermissionMatrix
}

export function emptyRoleModel(): RoleFormModel {
  return { id: '', name: '', description: '', matrix: { rows: [{ scope: '*', actions: ['browse', 'read'] }], global: [] } }
}

export function modelFromRole(role: Role): RoleFormModel {
  return { id: role.id, name: role.name, description: role.description, matrix: toMatrix(role.permissions) }
}

/** A role id suggested from its name: lower case, digits and `-`, starting with a letter. */
export function slugify(name: string): string {
  return name
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^[^a-z]+/, '')
    .replace(/-+$/, '')
    .slice(0, 48)
}

export const ROLE_ID_PATTERN = /^[a-z][a-z0-9-]{1,47}$/

/** Problems per matrix row (by index) and for the whole role: empty, bad scope syntax, duplicate scopes. */
export function matrixProblems(matrix: PermissionMatrix): { rows: Record<number, 'scope' | 'duplicate' | 'actions'>; empty: boolean } {
  const rows: Record<number, 'scope' | 'duplicate' | 'actions'> = {}
  const seen = new Set<string>()
  matrix.rows.forEach((row, i) => {
    const scope = row.scope.trim()
    if (scopeProblem(scope)) rows[i] = 'scope'
    else if (seen.has(scope.toLowerCase())) rows[i] = 'duplicate'
    else if (!row.actions.length) rows[i] = 'actions'
    seen.add(scope.toLowerCase())
  })
  return { rows, empty: fromMatrix(matrix).length === 0 }
}

/** Toggle an action in a row, keeping implications visible: write/delete/manage turn browse and read on; turning
 * browse or read off turns off what implies it. */
export function toggleAction(actions: RepoAction[], action: RepoAction, on: boolean): RepoAction[] {
  const set = new Set(actions)
  if (on) {
    set.add(action)
    if (!['browse', 'read'].includes(action)) {
      set.add('browse')
      set.add('read')
    }
  } else {
    set.delete(action)
    if (action === 'browse' || action === 'read') for (const a of ['write', 'delete', 'manage'] as const) set.delete(a)
  }
  return REPO_ACTIONS.filter((a) => set.has(a))
}

export function toRoleWrite(m: RoleFormModel): RoleWrite {
  return { name: m.name.trim(), description: m.description.trim(), permissions: fromMatrix(m.matrix) }
}

export function toRoleCreate(m: RoleFormModel): RoleCreate {
  return { id: m.id.trim(), ...toRoleWrite(m) }
}

// ---------------------------------------------------------------- service accounts
export interface AccountFormModel {
  name: string
  description: string
  roles: string[]
  disabled: boolean
}

export function toAccountCreate(m: AccountFormModel): ServiceAccountCreate {
  return { name: m.name.trim().toLowerCase(), description: m.description.trim(), roles: [...m.roles] }
}

export function toAccountUpdate(m: AccountFormModel, original: { description: string; roles: string[]; disabled: boolean }): ServiceAccountUpdate {
  const out: ServiceAccountUpdate = {}
  if (m.description.trim() !== original.description) out.description = m.description.trim()
  if (!sameSet(m.roles, original.roles)) out.roles = [...m.roles]
  if (m.disabled !== original.disabled) out.disabled = m.disabled
  return out
}

export const ACCOUNT_NAME_PATTERN = /^[a-z0-9][a-z0-9._-]{1,62}$/
