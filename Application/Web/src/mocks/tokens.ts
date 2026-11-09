// SPDX-License-Identifier: GPL-3.0-or-later
// Mock API tokens and service accounts (contract tag `tokens`): an in-memory store with seeded tokens, the same
// validation the server applies (scopes the owner holds, repository patterns, expiry in the future), secrets shown
// only in the creation response, 404 for unknown ids.
import { http, HttpResponse } from 'msw'

import type { ApiToken, ApiTokenCreate, FieldError, ServiceAccount, ServiceAccountCreate, ServiceAccountUpdate, TokenScope } from '@/api/types'

import { ROLES } from './fixtures/authProviders'

const API = '/api/v1'
export const ALL_SCOPES: TokenScope[] = ['read', 'write', 'delete', 'manage', 'stats-read', 'metrics', 'admin']
const PATTERN_RE = /^(?:format:[a-z0-9]+|type:(?:proxy|hosted|group)|[A-Za-z0-9._\-*?]{1,128})$/
const ACCOUNT_RE = /^[a-z0-9][a-z0-9._-]{1,62}$/
/** Role ids a service account may hold: the shared mock role list (built-in and custom). */
const roleIds = () => ROLES.map((r) => r.id)
const DAY = 86_400_000

export interface MockHelpers {
  guard: (request: Request) => Response | null
  problem: (status: number, title: string, detail?: string, errors?: FieldError[]) => Response
  notFound: () => Response
  latency: () => Promise<void>
  currentUser: () => string | null
}

interface StoredToken {
  token: ApiToken
  ownerKey: string // 'user:<name>' | 'sa:<id>'
}

const ago = (ms: number) => new Date(Date.now() - ms).toISOString()
const ahead = (ms: number) => new Date(Date.now() + ms).toISOString()
let seq = 0
const nextId = () => `01JC${String(Date.now()).slice(-10)}${String(++seq).padStart(12, '0')}`

export function randomSecret(): string {
  const bytes = new Uint8Array(32)
  crypto.getRandomValues(bytes)
  const b64 = btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `wst_${b64}`
}

function seedToken(name: string, scopes: TokenScope[], repositories: string[], extra: Partial<ApiToken>): ApiToken {
  return {
    id: nextId(),
    name,
    prefix: `wst_${Math.random().toString(36).slice(2, 10).padEnd(8, 'x')}`,
    owner: { kind: 'user', id: '01JC0000000000000000000ADM', name: 'admin' },
    scopes,
    repositories,
    status: 'active',
    expires_at: null,
    created_at: ago(40 * DAY),
    last_used_at: null,
    last_used_ip: null,
    revoked_at: null,
    ...extra,
  }
}

const tokens: StoredToken[] = [
  { ownerKey: 'user:*', token: seedToken('CI pipeline (build images)', ['read', 'write'], ['dockerhub-proxy', 'ghcr-proxy'], { expires_at: ahead(50 * DAY), last_used_at: ago(4 * 60_000), last_used_ip: '192.0.2.40' }) },
  { ownerKey: 'user:*', token: seedToken('Laptop docker login', ['read'], [], { created_at: ago(120 * DAY), last_used_at: ago(2 * DAY), last_used_ip: '198.51.100.23' }) },
  { ownerKey: 'user:*', token: seedToken('Prometheus scraper', ['metrics'], [], { created_at: ago(200 * DAY), last_used_at: ago(15_000), last_used_ip: '192.0.2.10' }) },
  { ownerKey: 'user:*', token: seedToken('Old build agent', ['read', 'write'], ['npm'], { created_at: ago(400 * DAY), expires_at: ago(35 * DAY), status: 'expired', last_used_at: ago(36 * DAY), last_used_ip: '192.0.2.77' }) },
  { ownerKey: 'user:*', token: seedToken('Leaked in a log', ['read'], [], { created_at: ago(90 * DAY), revoked_at: ago(60 * DAY), status: 'revoked', last_used_at: ago(61 * DAY), last_used_ip: '203.0.113.5' }) },
]

const accounts: ServiceAccount[] = [
  { id: '01JC00000000000000000SA001', name: 'ci-runner', description: 'Pipelines that build and push images', roles: ['deployer'], disabled: false, active_tokens: 0, created_at: ago(30 * DAY) },
  { id: '01JC00000000000000000SA002', name: 'npm-release', description: 'Publishes the shared npm packages from CI', roles: ['npm-publisher'], disabled: false, active_tokens: 0, created_at: ago(75 * DAY) },
  { id: '01JC00000000000000000SA003', name: 'legacy-backup', description: 'Old backup job (replaced)', roles: ['reader'], disabled: true, active_tokens: 0, created_at: ago(380 * DAY) },
]

/** The service accounts (the identity mocks count who holds a role and compute effective permissions from them). */
export const serviceAccounts = (): ServiceAccount[] => accounts

tokens.push(
  {
    ownerKey: 'sa:01JC00000000000000000SA001',
    token: seedToken('GitLab runner', ['read', 'write'], ['dockerhub-proxy', 'ghcr-proxy'], { owner: { kind: 'service_account', id: '01JC00000000000000000SA001', name: 'ci-runner' }, created_at: ago(28 * DAY), expires_at: ahead(62 * DAY), last_used_at: ago(11 * 60_000), last_used_ip: '192.0.2.41' }),
  },
  {
    ownerKey: 'sa:01JC00000000000000000SA002',
    token: seedToken('npm publish', ['read', 'write'], ['format:npm'], { owner: { kind: 'service_account', id: '01JC00000000000000000SA002', name: 'npm-release' }, created_at: ago(70 * DAY), expires_at: ahead(295 * DAY), last_used_at: ago(3 * DAY), last_used_ip: '192.0.2.42' }),
  },
)

/** Seeded tokens belong to whoever is signed in to the mock (`user:*`). */
function mine(user: string, stored: StoredToken): boolean {
  return stored.ownerKey === 'user:*' ? stored.token.owner.kind === 'user' : stored.ownerKey === `user:${user}`
}

function withOwnerName(stored: StoredToken, user: string): ApiToken {
  return stored.ownerKey === 'user:*' ? { ...stored.token, owner: { ...stored.token.owner, name: user } } : stored.token
}

function sorted(list: StoredToken[]): StoredToken[] {
  return [...list].sort((a, b) => b.token.created_at.localeCompare(a.token.created_at))
}

function validate(body: ApiTokenCreate, available: TokenScope[]): FieldError[] {
  const errors: FieldError[] = []
  if (!body.name?.trim()) errors.push({ field: 'name', message: 'A name is required.', code: 'required' })
  if (!body.scopes?.length) errors.push({ field: 'scopes', message: 'Choose at least one scope.', code: 'required' })
  body.scopes?.forEach((s, i) => {
    if (!available.includes(s)) errors.push({ field: `scopes.${i}`, message: `The owner does not hold '${s}'.`, code: 'scope' })
  })
  body.repositories?.forEach((p, i) => {
    if (p === '*' || !PATTERN_RE.test(p)) errors.push({ field: `repositories.${i}`, message: 'Use a repository name, a glob (docker-*), format:<format> or type:<type>.', code: 'pattern' })
  })
  if (body.expires_at && Date.parse(body.expires_at) <= Date.now() + 60_000) errors.push({ field: 'expires_at', message: 'Must be in the future.', code: 'past' })
  return errors
}

function newToken(body: ApiTokenCreate, owner: ApiToken['owner']): { token: ApiToken; secret: string } {
  const secret = randomSecret()
  const token: ApiToken = {
    id: nextId(),
    name: body.name.trim(),
    prefix: secret.slice(0, 12),
    owner,
    scopes: [...new Set(body.scopes)],
    repositories: [...new Set(body.repositories ?? [])],
    status: 'active',
    expires_at: body.expires_at ?? null,
    created_at: new Date().toISOString(),
    last_used_at: null,
    last_used_ip: null,
    revoked_at: null,
  }
  return { token, secret }
}

function revoke(stored: StoredToken): void {
  if (stored.token.status !== 'revoked') stored.token = { ...stored.token, status: 'revoked', revoked_at: new Date().toISOString() }
}

function accountTokens(id: string): StoredToken[] {
  return tokens.filter((s) => s.ownerKey === `sa:${id}`)
}

function accountBody(account: ServiceAccount): ServiceAccount {
  return { ...account, active_tokens: accountTokens(account.id).filter((s) => s.token.status === 'active').length }
}

function accountScopes(account: ServiceAccount): TokenScope[] {
  const perms = account.roles.flatMap((id) => ROLES.find((r) => r.id === id)?.permissions ?? [])
  if (perms.some((p) => p.action === 'admin')) return ALL_SCOPES
  const out = new Set<TokenScope>()
  for (const p of perms) {
    if (['browse', 'read', 'write', 'delete', 'manage'].includes(p.action)) out.add('read')
    if (['write', 'delete', 'manage'].includes(p.action)) out.add(p.action as TokenScope)
    if (p.action === 'stats-read') ['stats-read', 'metrics'].forEach((s) => out.add(s as TokenScope))
  }
  return ALL_SCOPES.filter((s) => out.has(s))
}

export function tokenHandlers(h: MockHelpers) {
  const userOf = () => h.currentUser() ?? 'admin'
  return [
    http.get(`${API}/auth/me/tokens`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const user = userOf()
      const items = sorted(tokens.filter((s) => mine(user, s))).map((s) => withOwnerName(s, user))
      return HttpResponse.json({ items, available_scopes: ALL_SCOPES })
    }),
    http.post(`${API}/auth/me/tokens`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const body = (await request.json()) as ApiTokenCreate
      const errors = validate(body, ALL_SCOPES)
      if (errors.length) return h.problem(422, 'Unprocessable Content', 'The request has invalid fields.', errors)
      const user = userOf()
      const created = newToken(body, { kind: 'user', id: '01JC0000000000000000000ADM', name: user })
      tokens.push({ ownerKey: `user:${user}`, token: created.token })
      return HttpResponse.json(created, { status: 201 })
    }),
    http.delete(`${API}/auth/me/tokens/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const stored = tokens.find((s) => s.token.id === params.id && mine(userOf(), s))
      if (!stored) return h.notFound()
      revoke(stored)
      return new HttpResponse(null, { status: 204 })
    }),
    http.get(`${API}/tokens`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      return HttpResponse.json(sorted(tokens).map((s) => withOwnerName(s, userOf())))
    }),
    http.delete(`${API}/tokens/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      const stored = tokens.find((s) => s.token.id === params.id)
      if (!stored) return h.notFound()
      revoke(stored)
      return new HttpResponse(null, { status: 204 })
    }),
    http.get(`${API}/service-accounts`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      return HttpResponse.json([...accounts].sort((a, b) => a.name.localeCompare(b.name)).map(accountBody))
    }),
    http.post(`${API}/service-accounts`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      const body = (await request.json()) as ServiceAccountCreate
      const name = body.name.trim().toLowerCase()
      const errors: FieldError[] = []
      if (!ACCOUNT_RE.test(name)) errors.push({ field: 'name', message: 'Lower case letters, digits, ".", "_" and "-".', code: 'pattern' })
      ;(body.roles ?? []).forEach((r, i) => {
        if (!roleIds().includes(r)) errors.push({ field: `roles.${i}`, message: `Unknown role '${r}'.`, code: 'role' })
      })
      if (errors.length) return h.problem(422, 'Unprocessable Content', 'The request has invalid fields.', errors)
      if (accounts.some((a) => a.name === name)) return h.problem(409, 'Conflict', 'A service account with this name already exists.')
      const account: ServiceAccount = { id: nextId(), name, description: body.description ?? '', roles: [...new Set(body.roles ?? [])], disabled: false, active_tokens: 0, created_at: new Date().toISOString() }
      accounts.push(account)
      return HttpResponse.json(accountBody(account), { status: 201 })
    }),
    http.patch(`${API}/service-accounts/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      const account = accounts.find((a) => a.id === params.id)
      if (!account) return h.notFound()
      const body = (await request.json()) as ServiceAccountUpdate
      if (body.description != null) account.description = body.description
      if (body.roles != null) account.roles = [...new Set(body.roles)]
      if (body.disabled != null) account.disabled = body.disabled
      return HttpResponse.json(accountBody(account))
    }),
    http.delete(`${API}/service-accounts/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      const index = accounts.findIndex((a) => a.id === params.id)
      if (index < 0) return h.notFound()
      accountTokens(String(params.id)).forEach(revoke)
      accounts.splice(index, 1)
      return new HttpResponse(null, { status: 204 })
    }),
    http.get(`${API}/service-accounts/:id/tokens`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      const account = accounts.find((a) => a.id === params.id)
      if (!account) return h.notFound()
      return HttpResponse.json({ items: sorted(accountTokens(account.id)).map((s) => s.token), available_scopes: accountScopes(account) })
    }),
    http.post(`${API}/service-accounts/:id/tokens`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      const account = accounts.find((a) => a.id === params.id)
      if (!account) return h.notFound()
      const body = (await request.json()) as ApiTokenCreate
      const errors = validate(body, accountScopes(account))
      if (errors.length) return h.problem(422, 'Unprocessable Content', 'The request has invalid fields.', errors)
      const created = newToken(body, { kind: 'service_account', id: account.id, name: account.name })
      tokens.push({ ownerKey: `sa:${account.id}`, token: created.token })
      return HttpResponse.json(created, { status: 201 })
    }),
    http.delete(`${API}/service-accounts/:id/tokens/:tokenId`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      const stored = accountTokens(String(params.id)).find((s) => s.token.id === params.tokenId)
      if (!stored) return h.notFound()
      revoke(stored)
      return new HttpResponse(null, { status: 204 })
    }),
  ]
}
