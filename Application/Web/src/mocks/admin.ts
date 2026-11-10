// SPDX-License-Identifier: GPL-3.0-or-later
// Mock identity administration (contract tag `admin`, T-U08): users, groups (local and provider-mapped), custom
// roles, anonymous access, effective permissions, a user's sessions, instance settings and system status. The rules
// the UI must show are modelled like the server: delegation bounded by the caller (403), nobody disables or deletes
// their own account and the last enabled administrator stays (409), built-in roles read-only, roles in use not
// deletable, mapped groups take role changes only.
import { http, HttpResponse } from 'msw'

import type {
  Action,
  AnonymousAccess,
  AuthProviderConfig,
  EffectivePermissions,
  EffectiveRole,
  FieldError,
  GrantedPermission,
  Group,
  GroupCreate,
  GroupMembersAdd,
  GroupUpdate,
  InstanceSettings,
  InstanceSettingsWrite,
  PasswordReset,
  RepositoryAccess,
  Role,
  RoleCreate,
  RoleDetail,
  RoleWrite,
  ServiceAccount,
  SessionInfo,
  SystemInfo,
  UserCreate,
  UserDetail,
  UserPage,
  UserSummary,
  UserUpdate,
} from '@/api/types'
import { GLOBAL_ACTIONS, impliedActions, isGlobalAction, REPO_ACTIONS, type RepoAction, repositoryOnly, scopeMatches, scopeProblem } from '@/utils/rbac'

import { type LocalGroupSeed, seedLocalGroups, seedSessions, seedUsers } from './fixtures/admin'
import type { MockHelpers } from './tokens'

const API = '/api/v1'
const USERNAME_RE = /^[a-z0-9][a-z0-9._@-]{0,127}$/
const GROUP_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$/
const ROLE_ID_RE = /^[a-z][a-z0-9-]{1,47}$/
const EMAIL_RE = /^[^@\s]{1,128}@[^@\s]{1,127}$/

/** A problem the handlers turn into problem+json. */
export class MockProblem extends Error {
  constructor(
    readonly status: number,
    readonly title: string,
    readonly detail?: string,
    readonly errors?: FieldError[],
  ) {
    super(detail ?? title)
  }
}

const invalid = (errors: FieldError[]) => new MockProblem(422, 'Unprocessable Content', 'The request has invalid fields.', errors)
const notFound = (what: string) => new MockProblem(404, 'Not Found', `${what} not found`)
const conflict = (detail: string) => new MockProblem(409, 'Conflict', detail)
const forbidden = (detail: string) => new MockProblem(403, 'Forbidden', detail)

export interface RepoRef {
  name: string
  format: string
  type: string
}

export interface AdminDeps {
  /** Built-in roles (shared with the provider mocks, which validate group mappings against them). */
  roles: Role[]
  providers: () => AuthProviderConfig[]
  repos: () => RepoRef[]
  serviceAccounts: () => ServiceAccount[]
}

/** The built-in `anonymous` role (not grantable, so not in the role list): browse and read on proxies. */
const ANONYMOUS_PERMISSIONS: GrantedPermission[] = [
  { action: 'browse', scope: 'type:proxy' },
  { action: 'read', scope: 'type:proxy' },
]

interface Grants {
  roles: Map<string, string[]>
  permissions: GrantedPermission[]
  global: Set<Action>
  admin: boolean
}

let seq = 0
const newId = () => `01JD${String(Date.now()).slice(-10)}${String(++seq).padStart(12, '0')}`

function encodeCursor(username: string): string {
  return btoa(username).replace(/=+$/, '')
}

function decodeCursor(cursor: string | null): string | null {
  if (!cursor) return null
  try {
    return atob(cursor)
  } catch {
    throw new MockProblem(400, 'Bad Request', 'invalid cursor')
  }
}

export class AdminWorld {
  readonly users: UserDetail[]
  readonly groups: LocalGroupSeed[]
  readonly sessions: Map<string, SessionInfo[]>
  anonymousRoles: string[] = ['anonymous']
  instanceName = 'Waystation'
  /** Settings → Two-factor sign-in: local administrators must set it up first (T-O08). */
  requireTotpForAdmins = false
  settingsUpdatedAt: string | null = null
  settingsUpdatedBy: string | null = null
  readonly startedAt: string

  constructor(
    readonly deps: AdminDeps,
    now = Date.now(),
  ) {
    this.users = seedUsers(now)
    this.groups = seedLocalGroups(now)
    this.sessions = seedSessions(this.users, now)
    this.startedAt = new Date(now - 3 * 86_400_000 - 4 * 3_600_000).toISOString()
  }

  // ---------------------------------------------------------------- roles and grants
  get roles(): Role[] {
    return this.deps.roles
  }

  role(id: string): Role | undefined {
    return this.roles.find((r) => r.id === id)
  }

  private grantsOf(roleSources: Map<string, string[]>): Grants {
    const perms = new Map<string, GrantedPermission>()
    const global = new Set<Action>()
    let admin = false
    for (const roleId of roleSources.keys()) {
      for (const p of roleId === 'anonymous' ? ANONYMOUS_PERMISSIONS : (this.role(roleId)?.permissions ?? [])) {
        if (p.action === 'admin') admin = true
        if (isGlobalAction(p.action)) global.add(p.action)
        const actions = isGlobalAction(p.action) ? [p.action] : impliedActions(p.action as RepoAction)
        for (const a of actions) perms.set(`${a} ${p.scope}`, { action: a, scope: p.scope })
      }
    }
    if (admin) for (const a of GLOBAL_ACTIONS) global.add(a)
    return { roles: roleSources, permissions: [...perms.values()], global, admin }
  }

  /** Mapped groups, derived from the providers' group mappings (one group per mapping row, like the server). */
  mappedGroups(): Group[] {
    return this.deps.providers().flatMap((p) =>
      p.group_mapping.map((m, i) => ({
        id: `${p.id}-${i}`,
        name: `${p.kind}:${p.name}:${m.external_group}`,
        description: '',
        source: p.kind,
        provider: { id: p.id, name: p.name },
        external_group: m.external_group,
        roles: [...m.roles],
        member_count: null,
        created_at: p.created_at,
        updated_at: p.updated_at,
      })),
    )
  }

  private localGroupBody(g: LocalGroupSeed): Group {
    return {
      id: g.id,
      name: g.name,
      description: g.description,
      source: 'local',
      provider: null,
      external_group: null,
      roles: [...g.roles],
      member_count: this.users.filter((u) => u.groups.includes(g.id)).length,
      created_at: g.created_at,
      updated_at: g.updated_at,
    }
  }

  allGroups(source?: string | null): Group[] {
    const local = [...this.groups].sort((a, b) => a.name.localeCompare(b.name)).map((g) => this.localGroupBody(g))
    const all = [...local, ...this.mappedGroups()]
    return source ? all.filter((g) => g.source === source) : all
  }

  /** Mapped groups a user is in: a group reported by the same provider whose id or name matches the mapping. */
  private mappedFor(user: UserDetail): Group[] {
    return this.mappedGroups().filter((g) =>
      user.external_groups.some((e) => {
        const kind = g.source
        if (e.provider !== `${kind}:${g.provider?.id}`) return false
        const want = (g.external_group ?? '').toLowerCase()
        return e.id.toLowerCase() === want || e.name.toLowerCase() === want
      }),
    )
  }

  roleSources(user: UserDetail): Map<string, string[]> {
    const via = new Map<string, string[]>()
    const add = (role: string, source: string) => via.set(role, [...(via.get(role) ?? []), source])
    for (const r of user.roles) add(r, 'direct')
    for (const g of this.groups.filter((g) => user.groups.includes(g.id))) for (const r of g.roles) add(r, g.name)
    for (const g of this.mappedFor(user)) for (const r of g.roles) add(r, g.name)
    return via
  }

  userGrants(user: UserDetail): Grants {
    return this.grantsOf(this.roleSources(user))
  }

  /** The signed-in mock user (unknown names act as administrators, like the default mock session). */
  callerGrants(username: string | null): Grants {
    const user = this.users.find((u) => u.username === username)
    return user ? this.userGrants(user) : this.grantsOf(new Map([['admin', ['direct']]]))
  }

  /** The roles of anonymous access as role sources (`via: anonymous`). */
  anonymousSources(): Map<string, string[]> {
    return new Map(this.anonymousRoles.map((r) => [r, ['anonymous']]))
  }

  /** Own plus anonymous grants: every signed-in principal also receives what anonymous access allows (repository
   * permissions only), so signing in never reduces access (SPEC §4.10). Delegation uses the own grants only. */
  withAnonymous(own: Grants): Grants {
    const anonymous = this.grantsOf(this.anonymousSources()).permissions.filter((p) => !isGlobalAction(p.action))
    const perms = new Map(own.permissions.map((p) => [`${p.action} ${p.scope}`, p]))
    for (const p of anonymous) perms.set(`${p.action} ${p.scope}`, p)
    return { ...own, permissions: [...perms.values()] }
  }

  /** What the signed-in mock user may do (`check`, capabilities): its own grants plus the anonymous ones. */
  effectiveGrants(username: string | null): Grants {
    return this.withAnonymous(this.callerGrants(username))
  }

  /** The server's `access.check`: may the user perform `action` (on `repo`; without one: on some repository)? */
  check(username: string | null, action: Action, repo?: RepoRef): boolean {
    const g = this.effectiveGrants(username)
    if (g.admin) return true
    if (isGlobalAction(action)) return g.global.has(action)
    return g.permissions.some((p) => p.action === action && (!repo || scopeMatches(p.scope, repo)))
  }

  /** A repository the user can neither browse nor read is hidden (404 everywhere). */
  canSee(username: string | null, repo: RepoRef): boolean {
    return this.check(username, 'browse', repo) || this.check(username, 'read', repo)
  }

  /** Session capabilities of a mock user, like the server's `access.capabilities` (UI hints only). */
  capabilities(username: string | null): string[] {
    const g = this.effectiveGrants(username)
    const caps: string[] = g.admin ? ['admin', 'repositories.create'] : []
    if (g.admin || g.permissions.some((p) => p.action === 'manage')) caps.push('repositories.manage')
    if (g.admin || g.permissions.some((p) => p.action === 'browse' || p.action === 'read')) caps.push('repositories.browse')
    const map: [Action, string][] = [
      ['stats-read', 'stats.read'],
      ['audit-read', 'audit.read'],
      ['user-admin', 'users.admin'],
      ['settings-admin', 'settings.admin'],
    ]
    for (const [action, cap] of map) if (g.global.has(action)) caps.push(cap)
    return caps
  }

  /** May the caller hand out these roles? Global actions only when held; `admin` only by administrators. */
  private mayAssign(caller: Grants, roleIds: string[]): boolean {
    if (caller.admin) return true
    return roleIds.every((id) => (this.role(id)?.permissions ?? []).every((p) => !isGlobalAction(p.action) || (p.action !== 'admin' && caller.global.has(p.action))))
  }

  /** May the caller change an account holding `target`? Not when the target holds more global power. */
  private mayManage(caller: Grants, target: Grants): boolean {
    if (caller.admin) return true
    if (target.admin) return false
    return [...target.global].every((a) => caller.global.has(a))
  }

  private checkRoles(caller: Grants, roles: string[], field: string, keep: string[] = []): FieldError[] {
    const errors: FieldError[] = []
    roles.forEach((r, i) => {
      if (!this.role(r)) errors.push({ field: `${field}.${i}`, message: `unknown role '${r}'`, code: 'role' })
      else if (!keep.includes(r) && !this.mayAssign(caller, [r])) errors.push({ field: `${field}.${i}`, message: `you cannot grant '${r}'`, code: 'role' })
    })
    return errors
  }

  // ---------------------------------------------------------------- users
  private summary(u: UserDetail): UserSummary {
    const { id, username, display_name, email, source, disabled, roles, groups, last_login, created_at, totp_enabled } = u
    const locked = Boolean(u.locked_until && Date.parse(u.locked_until) > Date.now())
    return { id, username, display_name, email, source, disabled, locked, totp_enabled, roles: [...roles], groups: [...groups], last_login, created_at }
  }

  detail(u: UserDetail): UserDetail {
    const local = this.groups.filter((g) => u.groups.includes(g.id)).map((g) => ({ id: g.id, name: g.name, source: 'local' }))
    const mapped = this.mappedFor(u).map((g) => ({ id: g.id, name: g.name, source: g.source }))
    return { ...u, ...this.summary(u), group_refs: [...local, ...mapped], effective_roles: [...this.roleSources(u).keys()] }
  }

  user(id: string): UserDetail {
    const u = this.users.find((x) => x.id === id)
    if (!u) throw notFound('user')
    return u
  }

  listUsers(params: URLSearchParams): UserPage {
    const q = params.get('q')?.trim().toLowerCase()
    const source = params.get('source')
    const disabled = params.get('disabled')
    const group = params.get('group')
    const role = params.get('role')
    const limit = Math.min(200, Math.max(1, Number(params.get('limit') ?? 50)))
    const after = decodeCursor(params.get('cursor'))
    const matches = this.users
      .filter((u) => !q || u.username.startsWith(q))
      .filter((u) => !source || u.source === source || u.source.startsWith(`${source}:`))
      .filter((u) => disabled === null || String(u.disabled) === disabled)
      .filter((u) => !group || u.groups.includes(group))
      .filter((u) => !role || u.roles.includes(role))
      .sort((a, b) => a.username.localeCompare(b.username))
      .filter((u) => after === null || u.username > after)
    const page = matches.slice(0, limit)
    return { items: page.map((u) => this.summary(u)), next_cursor: matches.length > limit ? encodeCursor(page.at(-1)!.username) : null }
  }

  /** Enabled administrators by direct role or local group (mapped groups do not count: a directory outage must not
   * lock everyone out). */
  private adminCount(): number {
    return this.users.filter((u) => {
      const via = new Map<string, string[]>()
      for (const r of u.roles) via.set(r, ['direct'])
      for (const g of this.groups.filter((g) => u.groups.includes(g.id))) for (const r of g.roles) via.set(r, [g.name])
      return !u.disabled && this.grantsOf(via).admin
    }).length
  }

  /** Apply `change` (which returns its undo); undo it and refuse (409) when it leaves no enabled administrator. */
  private keepingAnAdmin(change: () => () => void): void {
    const before = this.adminCount()
    const undo = change()
    if (before > 0 && this.adminCount() === 0) {
      undo()
      throw conflict('this would leave no enabled administrator')
    }
  }

  private manageable(caller: Grants, user: UserDetail): void {
    if (!this.mayManage(caller, this.userGrants(user))) throw forbidden('you cannot change a user that holds more permissions than you')
  }

  private checkGroups(caller: Grants, ids: string[], keep: string[]): FieldError[] {
    const errors: FieldError[] = []
    ids.forEach((id, i) => {
      const g = this.groups.find((x) => x.id === id)
      if (!g) errors.push({ field: `groups.${i}`, message: 'not a local group', code: 'group' })
      else if (!keep.includes(id) && !this.mayAssign(caller, g.roles)) errors.push({ field: `groups.${i}`, message: `you cannot grant '${g.name}'`, code: 'role' })
    })
    return errors
  }

  createUser(callerName: string | null, body: UserCreate): UserDetail {
    const caller = this.callerGrants(callerName)
    const username = (body.username ?? '').trim().toLowerCase()
    const errors: FieldError[] = []
    if (!USERNAME_RE.test(username)) errors.push({ field: 'username', message: 'lower case letters, digits, ".", "_", "@" and "-"', code: 'pattern' })
    if (body.password != null && body.password.length < 12) errors.push({ field: 'password', message: 'at least 12 characters', code: 'password' })
    if (body.password != null && body.password.toLowerCase() === username) errors.push({ field: 'password', message: 'must not be the username', code: 'password' })
    if (body.email && !EMAIL_RE.test(body.email)) errors.push({ field: 'email', message: 'an e-mail address (name@example.com)', code: 'email' })
    errors.push(...this.checkRoles(caller, body.roles ?? [], 'roles'), ...this.checkGroups(caller, body.groups ?? [], []))
    if (errors.length) throw invalid(errors)
    if (this.users.some((u) => u.username === username)) throw new MockProblem(409, 'Conflict', 'a user with this username already exists', [{ field: 'username', message: 'a user with this username already exists', code: 'conflict' }])
    const user: UserDetail = {
      id: newId(),
      username,
      display_name: body.display_name?.trim() || username,
      email: body.email?.trim() || null,
      source: 'local',
      disabled: false,
      locked: false,
      totp_enabled: false,
      roles: [...new Set(body.roles ?? [])],
      groups: [...new Set(body.groups ?? [])],
      last_login: null,
      created_at: new Date().toISOString(),
      locked_until: null,
      must_change_password: body.password != null && (body.must_change_password ?? true),
      has_password: body.password != null,
      group_refs: [],
      external_groups: [],
      identities: [],
      effective_roles: [],
    }
    this.users.push(user)
    return this.detail(user)
  }

  updateUser(callerName: string | null, id: string, body: UserUpdate): UserDetail {
    const caller = this.callerGrants(callerName)
    const before = this.user(id)
    this.manageable(caller, before)
    const errors: FieldError[] = []
    if (body.email && !EMAIL_RE.test(body.email)) errors.push({ field: 'email', message: 'an e-mail address (name@example.com)', code: 'email' })
    if (body.roles) errors.push(...this.checkRoles(caller, body.roles, 'roles', before.roles))
    if (body.groups) errors.push(...this.checkGroups(caller, body.groups, before.groups))
    if (errors.length) throw invalid(errors)
    if (body.disabled && before.username === callerName) throw new MockProblem(409, 'Conflict', 'you cannot disable your own account', [{ field: 'disabled', message: 'you cannot disable your own account', code: 'conflict' }])
    const proposed: UserDetail = {
      ...before,
      ...(body.display_name != null ? { display_name: body.display_name.trim() || before.username } : {}),
      ...('email' in body ? { email: body.email?.trim() || null } : {}),
      ...(body.roles != null ? { roles: [...new Set(body.roles)] } : {}),
      ...(body.groups != null ? { groups: [...new Set(body.groups)] } : {}),
      ...(body.disabled != null ? { disabled: body.disabled } : {}),
    }
    const previous = { ...before }
    this.keepingAnAdmin(() => {
      Object.assign(before, proposed)
      return () => Object.assign(before, previous)
    })
    if (proposed.disabled) this.sessions.delete(id)
    return this.detail(before)
  }

  deleteUser(callerName: string | null, id: string): void {
    const caller = this.callerGrants(callerName)
    const user = this.user(id)
    if (user.username === callerName) throw conflict('you cannot delete your own account')
    this.manageable(caller, user)
    this.keepingAnAdmin(() => {
      const index = this.users.indexOf(user)
      this.users.splice(index, 1)
      return () => this.users.splice(index, 0, user)
    })
    this.sessions.delete(id)
  }

  resetPassword(callerName: string | null, id: string, body: PasswordReset): void {
    const user = this.user(id)
    this.manageable(this.callerGrants(callerName), user)
    if (user.source !== 'local') throw conflict(`the account signs in through ${user.source}; its password is managed there`)
    if ((body.password ?? '').length < 12) throw invalid([{ field: 'password', message: 'at least 12 characters', code: 'password' }])
    user.has_password = true
    user.must_change_password = body.must_change_password ?? true
    this.sessions.delete(id)
  }

  unlock(callerName: string | null, id: string): UserDetail {
    const user = this.user(id)
    this.manageable(this.callerGrants(callerName), user)
    user.locked = false
    user.locked_until = null
    return this.detail(user)
  }

  /** An administrator's reset of a user's two-factor sign-in (lost phone); never one's own. */
  resetTotp(callerName: string | null, id: string): void {
    const user = this.user(id)
    this.manageable(this.callerGrants(callerName), user)
    if (user.username === callerName) throw new MockProblem(409, 'Conflict', 'turn off your own two-factor sign-in on your profile page')
    if (!user.totp_enabled) throw new MockProblem(409, 'Conflict', 'two-factor sign-in is not on for this user')
    user.totp_enabled = false
  }

  userSessions(id: string, callerName: string | null): SessionInfo[] {
    const user = this.user(id)
    return (this.sessions.get(id) ?? []).map((s, i) => ({ ...s, current: user.username === callerName && i === 0 }))
  }

  revokeSessions(callerName: string | null, id: string): number {
    const user = this.user(id)
    this.manageable(this.callerGrants(callerName), user)
    const count = this.sessions.get(id)?.length ?? 0
    this.sessions.delete(id)
    return count
  }

  // ---------------------------------------------------------------- effective permissions
  effective(principal: EffectivePermissions['principal'], own: Map<string, string[]>): EffectivePermissions {
    const grants = this.withAnonymous(this.grantsOf(own))
    const via = new Map(own)
    for (const [role, sources] of this.anonymousSources()) via.set(role, [...(via.get(role) ?? []), ...sources])
    const name = (id: string) => (id === 'anonymous' ? 'Anonymous' : (this.role(id)?.name ?? id))
    const roles: EffectiveRole[] = [...via].map(([id, sources]) => ({ id, name: name(id), via: sources }))
    const repositories: RepositoryAccess[] = []
    for (const repo of this.deps.repos()) {
      const actions = REPO_ACTIONS.filter((a) => grants.admin || grants.permissions.some((p) => p.action === a && scopeMatches(p.scope, repo)))
      if (actions.length) repositories.push({ name: repo.name, format: repo.format, type: repo.type, actions })
    }
    repositories.sort((a, b) => a.name.localeCompare(b.name))
    return { principal, roles, permissions: grants.permissions, global_actions: GLOBAL_ACTIONS.filter((a) => grants.global.has(a)), repositories, narrowed_by_token: false }
  }

  userPermissions(id: string): EffectivePermissions {
    const user = this.user(id)
    return this.effective({ kind: 'user', id: user.id, name: user.username }, this.roleSources(user))
  }

  accountPermissions(account: ServiceAccount): EffectivePermissions {
    return this.effective({ kind: 'service_account', id: account.id, name: account.name }, new Map(account.roles.map((r) => [r, ['direct']])))
  }

  // ---------------------------------------------------------------- groups
  group(id: string): Group {
    const found = this.allGroups().find((g) => g.id === id)
    if (!found) throw notFound('group')
    return found
  }

  createGroup(callerName: string | null, body: GroupCreate): Group {
    const caller = this.callerGrants(callerName)
    const name = (body.name ?? '').trim()
    const errors: FieldError[] = []
    if (!GROUP_NAME_RE.test(name)) errors.push({ field: 'name', message: 'letters, digits, spaces, ".", "_" and "-"', code: 'pattern' })
    errors.push(...this.checkRoles(caller, body.roles ?? [], 'roles'))
    if (errors.length) throw invalid(errors)
    if (this.groups.some((g) => g.name.toLowerCase() === name.toLowerCase())) throw new MockProblem(409, 'Conflict', 'a group with this name already exists', [{ field: 'name', message: 'a group with this name already exists', code: 'conflict' }])
    const now = new Date().toISOString()
    const group: LocalGroupSeed = { id: newId(), name, description: body.description ?? '', roles: [...new Set(body.roles ?? [])], created_at: now, updated_at: now }
    this.groups.push(group)
    return this.localGroupBody(group)
  }

  updateGroup(callerName: string | null, id: string, body: GroupUpdate): Group {
    const caller = this.callerGrants(callerName)
    const local = this.groups.find((g) => g.id === id)
    const mapped = local ? undefined : this.mappedGroups().find((g) => g.id === id)
    if (!local && !mapped) throw notFound('group')
    const current = local ? local.roles : mapped!.roles
    if (!this.mayAssign(caller, current)) throw forbidden('you cannot change a group that holds more permissions than you')
    if (mapped && (body.name != null || body.description != null)) throw conflict("a mapped group is managed by its provider's group mapping (only its roles can change here)")
    const errors: FieldError[] = []
    if (body.name != null && !GROUP_NAME_RE.test(body.name.trim())) errors.push({ field: 'name', message: 'letters, digits, spaces, ".", "_" and "-"', code: 'pattern' })
    if (body.roles) errors.push(...this.checkRoles(caller, body.roles, 'roles', current))
    if (errors.length) throw invalid(errors)
    if (local) {
      if (body.name != null && this.groups.some((g) => g.id !== id && g.name.toLowerCase() === body.name!.trim().toLowerCase())) throw conflict('a group with this name already exists')
      if (body.roles) {
        const roles = [...new Set(body.roles)]
        const previous = local.roles
        this.keepingAnAdmin(() => {
          local.roles = roles
          return () => (local.roles = previous)
        })
      }
      if (body.name != null) local.name = body.name.trim()
      if (body.description != null) local.description = body.description
      local.updated_at = new Date().toISOString()
      return this.localGroupBody(local)
    }
    const [providerId, index] = [mapped!.provider!.id, Number(id.slice(id.lastIndexOf('-') + 1))]
    const provider = this.deps.providers().find((p) => p.id === providerId)!
    if (body.roles) provider.group_mapping[index]!.roles = [...new Set(body.roles)]
    return this.group(id)
  }

  deleteGroup(callerName: string | null, id: string): void {
    const local = this.groups.find((g) => g.id === id)
    if (!local) {
      if (this.mappedGroups().some((g) => g.id === id)) throw conflict("a mapped group is removed from its provider's group mapping")
      throw notFound('group')
    }
    if (!this.mayAssign(this.callerGrants(callerName), local.roles)) throw forbidden('you cannot change a group that holds more permissions than you')
    this.keepingAnAdmin(() => {
      const index = this.groups.indexOf(local)
      this.groups.splice(index, 1)
      return () => this.groups.splice(index, 0, local)
    })
    for (const u of this.users) u.groups = u.groups.filter((g) => g !== id)
  }

  members(id: string, params: URLSearchParams): UserPage {
    const group = this.group(id)
    if (group.source !== 'local') return { items: [], next_cursor: null }
    const sp = new URLSearchParams(params)
    sp.set('group', id)
    return this.listUsers(sp)
  }

  addMembers(callerName: string | null, id: string, body: GroupMembersAdd): void {
    const local = this.groups.find((g) => g.id === id)
    if (!local) {
      if (this.mappedGroups().some((g) => g.id === id)) throw conflict('members of a mapped group come from its provider')
      throw notFound('group')
    }
    const caller = this.callerGrants(callerName)
    if (!this.mayAssign(caller, local.roles)) throw forbidden(`you cannot grant '${local.name}'`)
    const missing: FieldError[] = []
    body.user_ids.forEach((uid, i) => {
      if (!this.users.some((u) => u.id === uid)) missing.push({ field: `user_ids.${i}`, message: 'unknown user', code: 'user' })
    })
    if (missing.length) throw invalid(missing)
    for (const uid of body.user_ids) {
      const user = this.user(uid)
      this.manageable(caller, user)
      if (!user.groups.includes(id)) user.groups.push(id)
    }
  }

  removeMember(callerName: string | null, id: string, userId: string): void {
    const local = this.groups.find((g) => g.id === id)
    if (!local) throw notFound('group')
    const user = this.user(userId)
    if (!user.groups.includes(id)) throw notFound('membership')
    this.manageable(this.callerGrants(callerName), user)
    const previous = user.groups
    this.keepingAnAdmin(() => {
      user.groups = previous.filter((g) => g !== id)
      return () => (user.groups = previous)
    })
  }

  // ---------------------------------------------------------------- roles
  usage(id: string): RoleDetail['usage'] {
    return {
      users: this.users.filter((u) => u.roles.includes(id)).length,
      groups: this.allGroups().filter((g) => g.roles.includes(id)).length,
      service_accounts: this.deps.serviceAccounts().filter((a) => a.roles.includes(id)).length,
    }
  }

  roleDetail(id: string): RoleDetail {
    const role = this.role(id)
    if (!role) throw notFound('role')
    return { ...role, usage: role.builtin ? { users: 0, groups: 0, service_accounts: 0 } : this.usage(id) }
  }

  private checkPermissions(caller: Grants, body: RoleWrite, keep: Role['permissions'] = []): FieldError[] {
    const errors: FieldError[] = []
    if (!body.name?.trim()) errors.push({ field: 'name', message: 'a name is required', code: 'required' })
    if (!body.permissions?.length) errors.push({ field: 'permissions', message: 'add at least one permission', code: 'required' })
    body.permissions?.forEach((p, i) => {
      if (isGlobalAction(p.action) && p.scope !== '*') errors.push({ field: `permissions.${i}.scope`, message: 'global actions are granted only on scope *', code: 'scope' })
      else if (scopeProblem(p.scope)) errors.push({ field: `permissions.${i}.scope`, message: 'use *, a repository name, a glob (docker-*), format:<format> or type:<proxy|hosted|group>', code: 'scope' })
    })
    const added = (body.permissions ?? []).filter((p) => !keep.some((k) => k.action === p.action && k.scope === p.scope))
    if (!caller.admin && added.some((p) => isGlobalAction(p.action) && (p.action === 'admin' || !caller.global.has(p.action)))) {
      errors.push({ field: 'permissions', message: 'you can only add global permissions you hold yourself (admin: administrators only)', code: 'delegation' })
    }
    return errors
  }

  createRole(callerName: string | null, body: RoleCreate): Role {
    const caller = this.callerGrants(callerName)
    const errors: FieldError[] = []
    if (!ROLE_ID_RE.test(body.id ?? '')) errors.push({ field: 'id', message: 'lower-case letters, digits and "-"', code: 'pattern' })
    errors.push(...this.checkPermissions(caller, body))
    if (errors.length) throw invalid(errors)
    const existing = this.role(body.id)
    if (existing) throw new MockProblem(409, 'Conflict', existing.builtin ? 'a built-in role has this id' : 'a role with this id already exists', [{ field: 'id', message: 'already taken', code: 'conflict' }])
    const role: Role = { id: body.id, name: body.name.trim(), description: body.description ?? '', builtin: false, permissions: dedupe(body.permissions) }
    this.roles.push(role)
    return role
  }

  updateRole(callerName: string | null, id: string, body: RoleWrite): Role {
    const role = this.role(id)
    if (!role) throw notFound('role')
    if (role.builtin) throw conflict('built-in roles are read-only')
    const caller = this.callerGrants(callerName)
    if (!caller.admin && role.permissions.some((p) => isGlobalAction(p.action) && !caller.global.has(p.action))) throw forbidden('you cannot change a role holding more permissions than you')
    const errors = this.checkPermissions(caller, body, role.permissions)
    if (errors.length) throw invalid(errors)
    Object.assign(role, { name: body.name.trim(), description: body.description ?? '', permissions: dedupe(body.permissions) })
    return role
  }

  deleteRole(callerName: string | null, id: string): void {
    const role = this.role(id)
    if (!role) throw notFound('role')
    if (role.builtin) throw conflict('built-in roles are read-only')
    const caller = this.callerGrants(callerName)
    if (!caller.admin && role.permissions.some((p) => isGlobalAction(p.action) && !caller.global.has(p.action))) throw forbidden('you cannot delete a role holding more permissions than you')
    const u = this.usage(id)
    const holders = [u.users && `${u.users} user(s)`, u.groups && `${u.groups} group(s)`, u.service_accounts && `${u.service_accounts} service account(s)`].filter(Boolean)
    if (holders.length) throw conflict(`the role is still held by ${holders.join(', ')}; remove it from them first`)
    this.roles.splice(this.roles.indexOf(role), 1)
  }

  // ---------------------------------------------------------------- anonymous access
  setAnonymous(body: AnonymousAccess): AnonymousAccess {
    const errors: FieldError[] = []
    body.roles.forEach((r, i) => {
      const role = r === 'anonymous' ? { permissions: [] } : this.role(r)
      if (!role) errors.push({ field: `roles.${i}`, message: `unknown role '${r}'`, code: 'role' })
      else if (!repositoryOnly(role.permissions)) errors.push({ field: `roles.${i}`, message: 'anonymous access can only use roles holding repository permissions', code: 'role' })
    })
    if (errors.length) throw invalid(errors)
    this.anonymousRoles = [...new Set(body.roles)]
    return { roles: [...this.anonymousRoles] }
  }

  // ---------------------------------------------------------------- settings and system
  settings(): InstanceSettings {
    return {
      general: { instance_name: this.instanceName },
      security: { require_totp_for_admins: this.requireTotpForAdmins },
      limits: {
        session_idle_minutes: 480,
        session_max_hours: 168,
        login_rate_limit: 20,
        login_lockout_failures: 10,
        login_lockout_minutes: 15,
        token_failure_limit: 60,
        audit_retention_days: 400,
        stats_minute_retention_days: 7,
        max_upload_bytes: 64 * 1024 ** 3,
        min_free_bytes: 5 * 1024 ** 3,
      },
      updated_at: this.settingsUpdatedAt,
      updated_by: this.settingsUpdatedBy,
    }
  }

  saveSettings(callerName: string | null, body: InstanceSettingsWrite): InstanceSettings {
    const name = body.general?.instance_name?.trim() ?? ''
    if (!name || name.length > 64) throw invalid([{ field: 'general.instance_name', message: 'between 1 and 64 characters', code: 'length' }])
    this.instanceName = name
    if (body.security) this.requireTotpForAdmins = body.security.require_totp_for_admins
    this.settingsUpdatedAt = new Date().toISOString()
    this.settingsUpdatedBy = callerName
    return this.settings()
  }

  system(storage: { total: number; used: number }): SystemInfo {
    const jitter = () => Math.round((0.3 + Math.random() * 1.4) * 100) / 100
    return {
      version: '0.1.0-m0 (mock)',
      python: '3.14.0',
      started_at: this.startedAt,
      components: [
        { name: 'mongodb', status: 'ok', version: '8.2.1', latency_ms: jitter(), detail: null },
        { name: 'valkey', status: 'ok', version: '9.0.0', latency_ms: jitter() / 2, detail: null },
      ],
      storage: { path: '/var/lib/waystation', total_bytes: storage.total, used_bytes: storage.used, free_bytes: storage.total - storage.used, min_free_bytes: 5 * 1024 ** 3 },
    }
  }
}

function dedupe(perms: Role['permissions']): Role['permissions'] {
  const seen = new Set<string>()
  return perms.filter((p) => {
    const key = `${p.action} ${p.scope}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

export interface AdminMockHelpers extends MockHelpers {
  storage: () => { total: number; used: number }
}

/** MSW handlers over an AdminWorld. */
export function adminHandlers(h: AdminMockHelpers, world: AdminWorld) {
  const send = (error: unknown) => {
    if (error instanceof MockProblem) return h.problem(error.status, error.title, error.detail, error.errors)
    throw error
  }
  /** Guard, latency, then run `fn` turning MockProblems into problem+json. */
  const route =
    (fn: (ctx: { request: Request; params: Record<string, string | readonly string[] | undefined>; caller: string | null }) => Promise<Response> | Response) =>
    async ({ request, params }: { request: Request; params: Record<string, string | readonly string[] | undefined> }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      try {
        return await fn({ request, params, caller: h.currentUser() })
      } catch (error) {
        return send(error)
      }
    }
  const id = (params: Record<string, string | readonly string[] | undefined>, key: string) => String(params[key])
  const noContent = () => new HttpResponse(null, { status: 204 })
  const json = <T>(request: Request) => request.json() as Promise<T>

  return [
    http.get(`${API}/users`, route(({ request }) => HttpResponse.json(world.listUsers(new URL(request.url).searchParams)))),
    http.post(`${API}/users`, route(async ({ request, caller }) => HttpResponse.json(world.createUser(caller, await json<UserCreate>(request)), { status: 201 }))),
    http.get(`${API}/users/:id`, route(({ params }) => HttpResponse.json(world.detail(world.user(id(params, 'id')))))),
    http.patch(`${API}/users/:id`, route(async ({ request, params, caller }) => HttpResponse.json(world.updateUser(caller, id(params, 'id'), await json<UserUpdate>(request))))),
    http.delete(`${API}/users/:id`, route(({ params, caller }) => (world.deleteUser(caller, id(params, 'id')), noContent()))),
    http.post(`${API}/users/:id/password`, route(async ({ request, params, caller }) => (world.resetPassword(caller, id(params, 'id'), await json<PasswordReset>(request)), noContent()))),
    http.post(`${API}/users/:id/unlock`, route(({ params, caller }) => HttpResponse.json(world.unlock(caller, id(params, 'id'))))),
    http.delete(`${API}/users/:id/totp`, route(({ params, caller }) => (world.resetTotp(caller, id(params, 'id')), noContent()))),
    http.get(`${API}/users/:id/permissions`, route(({ params }) => HttpResponse.json(world.userPermissions(id(params, 'id'))))),
    http.get(`${API}/users/:id/sessions`, route(({ params, caller }) => HttpResponse.json(world.userSessions(id(params, 'id'), caller)))),
    http.delete(`${API}/users/:id/sessions`, route(({ params, caller }) => HttpResponse.json({ revoked: world.revokeSessions(caller, id(params, 'id')) }))),

    http.get(`${API}/groups`, route(({ request }) => HttpResponse.json(world.allGroups(new URL(request.url).searchParams.get('source'))))),
    http.post(`${API}/groups`, route(async ({ request, caller }) => HttpResponse.json(world.createGroup(caller, await json<GroupCreate>(request)), { status: 201 }))),
    http.get(`${API}/groups/:id`, route(({ params }) => HttpResponse.json(world.group(id(params, 'id'))))),
    http.patch(`${API}/groups/:id`, route(async ({ request, params, caller }) => HttpResponse.json(world.updateGroup(caller, id(params, 'id'), await json<GroupUpdate>(request))))),
    http.delete(`${API}/groups/:id`, route(({ params, caller }) => (world.deleteGroup(caller, id(params, 'id')), noContent()))),
    http.get(`${API}/groups/:id/members`, route(({ request, params }) => HttpResponse.json(world.members(id(params, 'id'), new URL(request.url).searchParams)))),
    http.post(`${API}/groups/:id/members`, route(async ({ request, params, caller }) => (world.addMembers(caller, id(params, 'id'), await json<GroupMembersAdd>(request)), noContent()))),
    http.delete(`${API}/groups/:id/members/:userId`, route(({ params, caller }) => (world.removeMember(caller, id(params, 'id'), id(params, 'userId')), noContent()))),

    http.get(`${API}/roles`, route(() => HttpResponse.json(world.roles))),
    http.post(`${API}/roles`, route(async ({ request, caller }) => HttpResponse.json(world.createRole(caller, await json<RoleCreate>(request)), { status: 201 }))),
    http.get(`${API}/roles/:id`, route(({ params }) => HttpResponse.json(world.roleDetail(id(params, 'id'))))),
    http.put(`${API}/roles/:id`, route(async ({ request, params, caller }) => HttpResponse.json(world.updateRole(caller, id(params, 'id'), await json<RoleWrite>(request))))),
    http.delete(`${API}/roles/:id`, route(({ params, caller }) => (world.deleteRole(caller, id(params, 'id')), noContent()))),

    http.get(`${API}/anonymous-access`, route(() => HttpResponse.json({ roles: world.anonymousRoles }))),
    http.put(`${API}/anonymous-access`, route(async ({ request }) => HttpResponse.json(world.setAnonymous(await json<AnonymousAccess>(request))))),

    http.get(`${API}/service-accounts/:id`, route(({ params }) => {
      const account = world.deps.serviceAccounts().find((a) => a.id === id(params, 'id'))
      if (!account) throw notFound('service account')
      return HttpResponse.json(account)
    })),
    http.get(`${API}/service-accounts/:id/permissions`, route(({ params }) => {
      const account = world.deps.serviceAccounts().find((a) => a.id === id(params, 'id'))
      if (!account) throw notFound('service account')
      return HttpResponse.json(world.accountPermissions(account))
    })),

    http.get(`${API}/settings`, route(() => HttpResponse.json(world.settings()))),
    http.put(`${API}/settings`, route(async ({ request, caller }) => HttpResponse.json(world.saveSettings(caller, await json<InstanceSettingsWrite>(request))))),
    http.get(`${API}/system`, route(() => HttpResponse.json(world.system(h.storage())))),
  ]
}
