// SPDX-License-Identifier: GPL-3.0-or-later
// Repository "Members" tab (T-U04): which roles reach a repository and with which actions, worked out from the role
// definitions with the same scope rules as the server (`utils/rbac.ts`). Display only: the RBAC engine decides.
import type { Repository, Role } from '@/api/types'
import { impliedActions, isGlobalAction, REPO_ACTIONS, type RepoAction, scopeMatches } from '@/utils/rbac'

export interface RoleReach {
  role: Role
  /** Repository actions the role grants here (implied ones included), in REPO_ACTIONS order. */
  actions: RepoAction[]
  /** The role holds the global `admin` action: everything, everywhere. */
  admin: boolean
  /** The scopes that matched this repository (for "via `docker-*`"). */
  scopes: string[]
}

/** Roles that grant something on `repo`, administrators first, then by how much they grant. */
export function rolesReaching(roles: Role[], repo: Pick<Repository, 'name' | 'format' | 'type'>): RoleReach[] {
  const out: RoleReach[] = []
  for (const role of roles) {
    const admin = role.permissions.some((p) => p.action === 'admin')
    const granted = new Set<RepoAction>()
    const scopes = new Set<string>()
    for (const p of role.permissions) {
      if (isGlobalAction(p.action) || !scopeMatches(p.scope, repo)) continue
      for (const a of impliedActions(p.action as RepoAction)) granted.add(a)
      scopes.add(p.scope)
    }
    if (admin) for (const a of REPO_ACTIONS) granted.add(a)
    if (!granted.size) continue
    out.push({ role, actions: REPO_ACTIONS.filter((a) => granted.has(a)), admin, scopes: [...scopes] })
  }
  return out.sort((a, b) => Number(b.admin) - Number(a.admin) || b.actions.length - a.actions.length || a.role.name.localeCompare(b.role.name))
}
