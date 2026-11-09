// SPDX-License-Identifier: GPL-3.0-or-later
// RBAC vocabulary for the administration pages (SPEC §4.10): actions, scopes, the permission matrix of a role and
// presentation of user sources. Display only: the server's RBAC engine decides every request.
import type { Action, RolePermission } from '@/api/types'

export const REPO_ACTIONS = ['browse', 'read', 'write', 'delete', 'manage'] as const satisfies readonly Action[]
export const GLOBAL_ACTIONS = ['admin', 'user-admin', 'audit-read', 'stats-read', 'settings-admin'] as const satisfies readonly Action[]
export type RepoAction = (typeof REPO_ACTIONS)[number]
export type GlobalAction = (typeof GLOBAL_ACTIONS)[number]

const REPO_SET = new Set<string>(REPO_ACTIONS)
const SCOPE_RE = /^(?:\*|format:[a-z0-9]+|type:(?:proxy|hosted|group)|[A-Za-z0-9._\-*?[\]]{1,128})$/

export function isGlobalAction(action: Action): action is GlobalAction {
  return !REPO_SET.has(action)
}

/** `write`, `delete` and `manage` imply `browse` and `read` on the same scope. */
export function impliedActions(action: RepoAction): RepoAction[] {
  return action === 'browse' || action === 'read' ? [action] : ['browse', 'read', action]
}

/** A client-side check of a scope's syntax (the server validates again). */
export function scopeProblem(scope: string): string | null {
  const s = scope.trim()
  if (!s) return 'required'
  return SCOPE_RE.test(s) ? null : 'pattern'
}

export type ScopeKind = 'all' | 'format' | 'type' | 'glob' | 'name'

export function scopeKind(scope: string): ScopeKind {
  if (scope === '*') return 'all'
  if (scope.startsWith('format:')) return 'format'
  if (scope.startsWith('type:')) return 'type'
  return /[*?[]/.test(scope) ? 'glob' : 'name'
}

function globToRegExp(glob: string): RegExp {
  let out = ''
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i]!
    if (c === '*') out += '.*'
    else if (c === '?') out += '.'
    else if (c === '[') {
      const end = glob.indexOf(']', i + 1)
      if (end < 0) out += '\\['
      else {
        const body = glob.slice(i + 1, end).replace(/\\/g, '\\\\')
        out += body.startsWith('!') ? `[^${body.slice(1)}]` : `[${body}]`
        i = end
      }
    } else out += c.replace(/[.+^${}()|\\/]/g, '\\$&')
  }
  return new RegExp(`^${out}$`)
}

/** Whether a permission scope covers a repository (`*`, its name, a glob, `format:<f>`, `type:<t>`). */
export function scopeMatches(scope: string, repo: { name: string; format: string; type: string }): boolean {
  switch (scopeKind(scope)) {
    case 'all':
      return true
    case 'format':
      return repo.format === scope.slice(7)
    case 'type':
      return repo.type === scope.slice(5)
    case 'glob':
      return globToRegExp(scope.toLowerCase()).test(repo.name.toLowerCase())
    default:
      return scope.toLowerCase() === repo.name.toLowerCase()
  }
}

/** A role's permissions as a matrix: one row per scope, the repository actions held on it; global actions apart. */
export interface PermissionMatrix {
  rows: { scope: string; actions: RepoAction[] }[]
  global: GlobalAction[]
}

export function toMatrix(permissions: readonly RolePermission[]): PermissionMatrix {
  const rows = new Map<string, Set<RepoAction>>()
  const global = new Set<GlobalAction>()
  for (const p of permissions) {
    if (isGlobalAction(p.action)) global.add(p.action)
    else {
      const set = rows.get(p.scope) ?? new Set<RepoAction>()
      set.add(p.action as RepoAction)
      rows.set(p.scope, set)
    }
  }
  return {
    rows: [...rows].map(([scope, set]) => ({ scope, actions: REPO_ACTIONS.filter((a) => set.has(a)) })),
    global: GLOBAL_ACTIONS.filter((a) => global.has(a)),
  }
}

/** Back to `(action, scope)` pairs: rows in order (empty scopes and rows without actions dropped, duplicate scopes
 * merged), then global actions on `*`. */
export function fromMatrix(matrix: PermissionMatrix): RolePermission[] {
  const out: RolePermission[] = []
  const seen = new Set<string>()
  for (const row of matrix.rows) {
    const scope = row.scope.trim()
    if (!scope) continue
    for (const action of REPO_ACTIONS) {
      if (!row.actions.includes(action) || seen.has(`${action} ${scope}`)) continue
      seen.add(`${action} ${scope}`)
      out.push({ action, scope })
    }
  }
  for (const action of GLOBAL_ACTIONS) if (matrix.global.includes(action)) out.push({ action, scope: '*' })
  return out
}

/** Whether a set of permissions carries no global action (what anonymous access may use). */
export function repositoryOnly(permissions: readonly RolePermission[]): boolean {
  return permissions.every((p) => !isGlobalAction(p.action))
}

export type SourceKind = 'local' | 'ldap' | 'oidc'

/** `local`, `ldap:<provider>` or `oidc:<provider>` → kind and provider id. */
export function parseSource(source: string): { kind: SourceKind; provider: string | null } {
  const [kind, ...rest] = source.split(':')
  if (kind === 'ldap' || kind === 'oidc') return { kind, provider: rest.join(':') || null }
  return { kind: 'local', provider: null }
}
