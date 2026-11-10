// SPDX-License-Identifier: GPL-3.0-or-later
// Mock RBAC: the server's route rules (`security.access`, SPEC §4.10) for the routes the UI calls, so mock mode shows
// what a user with few or no permissions sees (sign in as `nina`: no role, so only what anonymous access allows, which
// every signed-in user also gets (turn anonymous access off to see the "no access yet" state); `reed`: reader; `gus`:
// a role with the anonymous grants only). One MSW handler in front of all the others answers 403 `permission_required` for a missing global
// action and 404 for a hidden repository; everything else falls through to the real mock handlers.
import { http, HttpResponse } from 'msw'

import type { Action, Problem } from '@/api/types'

import type { RepoRef } from './admin'

const API = '/api/v1'

export interface AccessWorld {
  check(username: string | null, action: Action, repo?: RepoRef): boolean
  canSee(username: string | null, repo: RepoRef): boolean
}

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' | '*'
/** A global rule: `null` = no global action needed here (the handler decides, like `handler_checked`). `manage-any`:
 * `manage` on some repository (the cleanup policies' read side). */
type Need = Action | 'manage-any' | null

/** First match wins. Paths are relative to /api/v1. Routes that are not listed need only a session. */
const GLOBAL_RULES: [Method, RegExp, Need][] = [
  ['GET', /^\/stats\/(summary|traffic|storage|upstreams)$/, 'stats-read'],
  ['GET', /^\/(transfers|activity|activity\/export)$/, 'stats-read'],
  ['*', /^\/(tasks|maintenance|webhooks|provisioning|import)(\/|$)/, 'admin'],
  ['*', /^\/system$/, 'admin'],
  ['POST', /^\/repositories$/, 'admin'],
  ['*', /^\/(users|groups|service-accounts|tokens|anonymous-access)(\/|$)/, 'user-admin'],
  ['*', /^\/roles\/.+/, 'user-admin'],
  ['POST', /^\/roles$/, 'user-admin'],
  ['*', /^\/audit(\/|$)/, 'audit-read'],
  ['*', /^\/(settings|auth-providers)(\/|$)/, 'settings-admin'],
  ['PUT', /^\/storage\/guard$/, 'settings-admin'],
  ['POST', /^\/cleanup-policies\/preview$/, null],
  ['GET', /^\/cleanup-policies(\/|$)/, 'manage-any'],
  ['*', /^\/cleanup-policies(\/|$)/, 'admin'],
]

/** `/repositories/{name}/...` (not the collection routes): the action the route needs on that repository. */
const REPO_ROUTE = /^\/repositories\/(?!test-upstream$|import\/)([^/]+)(\/.*)?$/

function repoAction(method: string, rest: string): Action {
  if (rest === '/cleanup' || ((method === 'PUT' || method === 'DELETE') && rest === '')) return 'manage'
  if (rest === '/pypi/yank') return 'write'
  return 'browse'
}

function refusal(status: 403 | 404, detail: string) {
  const body: Problem = { type: 'about:blank', title: status === 403 ? 'Forbidden' : 'Not Found', status, detail, ...(status === 403 ? { code: 'permission_required' } : {}) }
  return HttpResponse.json(body, { status, headers: { 'Content-Type': 'application/problem+json' } })
}

/** The rule's verdict for `user`: null when allowed (or the handler decides). */
export function decide(world: AccessWorld, user: string, method: string, path: string, repos: readonly RepoRef[]): Response | null {
  const repoMatch = REPO_ROUTE.exec(path)
  if (repoMatch) {
    const repo = repos.find((r) => r.name === decodeURIComponent(repoMatch[1]!))
    if (!repo) return null // the handler answers 404 for unknown repositories
    if (!world.canSee(user, repo)) return refusal(404, 'The requested object does not exist or is not visible to you.')
    const action = repoAction(method, repoMatch[2] ?? '')
    return world.check(user, action, repo) ? null : refusal(403, `${action} permission required on this repository`)
  }
  for (const [m, pattern, need] of GLOBAL_RULES) {
    if ((m !== '*' && m !== method) || !pattern.test(path)) continue
    if (need === null) return null
    if (need === 'manage-any') return world.check(user, 'admin') || world.check(user, 'manage') ? null : refusal(403, 'admin permission required')
    return world.check(user, need) ? null : refusal(403, `${need} permission required`)
  }
  return null
}

/** The front handler: refuses what the signed-in mock user may not do, else falls through (returns undefined). */
export function accessHandler(world: AccessWorld, currentUser: () => string | null, repos: () => readonly RepoRef[]) {
  return http.all(`${API}/*`, ({ request }) => {
    const user = currentUser()
    if (!user) return undefined // the handlers' own guard answers 401
    const path = new URL(request.url).pathname.slice(API.length)
    return decide(world, user, request.method, path, repos()) ?? undefined
  })
}
