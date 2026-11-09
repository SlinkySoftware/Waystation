// SPDX-License-Identifier: GPL-3.0-or-later
// MSW handlers implementing the M0 contract (openapi/waystation-m0.yaml) against the mock world.
// Behaviour the UI depends on is modelled: problem+json errors with field errors, 404 for unknown objects,
// CSRF header on unsafe methods, cursor pagination, realistic latency.
import { delay, http, HttpResponse } from 'msw'

import type { AuthProviderTestRequest, AuthProviderWrite, FieldError, Format, PasswordChange, Preferences, Problem, PypiYank, Repository, RepositoryWrite, Session, StatsRange, StorageGuard, StorageGuardSettings } from '@/api/types'
import { M0_FORMATS } from '@/api/types'

import { activityCsv, activityPage, activityQuery } from './activity'
import { adminHandlers, AdminWorld } from './admin'
import { auditHandlers, recordAudit } from './audit'
import { ADMIN_USER_ID, CUSTOM_ROLES } from './fixtures/admin'
import { aptPackagesFor, aptSuitesFor } from './fixtures/apt'
import { ROLES, seedProviders, testConnection, testUser, toConfig, validateProvider } from './fixtures/authProviders'
import { dockerImagesFor, dockerManifestFor, dockerTagsFor } from './fixtures/docker'
import { npmPackageList, npmVersionsFor } from './fixtures/npm'
import { goModuleList, goVersionsFor } from './fixtures/go'
import { mavenArtifactsFor, mavenListing, mavenVersionsFor } from './fixtures/maven'
import { pypiProjectList, pypiVersionsFor, pypiYank } from './fixtures/pypi'
import { genericTree, rawListing } from './fixtures/raw'
import { BASE_URL, quotaView, REGISTRY_HOST } from './fixtures/repositories'
import { cleanupHandlers } from './cleanup'
import { nexusImportHandlers } from './nexusImport'
import { tasksHandlers } from './tasks'
import { setupGuide } from './setup'
import { mockSearch, type SearchParams } from './search'
import { RANGES, statsSummary, trafficSeries } from './stats'
import { serviceAccounts, tokenHandlers } from './tokens'
import { upstreamHealth, world } from './world'

const API = '/api/v1'
const SESSION_KEY = 'ws.mock.session'
const CSRF = 'mock-csrf-7f3a9c'
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$/

let preferences: Preferences = { theme: 'system', density: 'comfortable' }
const providers = seedProviders()
ROLES.push(...CUSTOM_ROLES.map((r) => ({ ...r, permissions: [...r.permissions] })))
const admin = new AdminWorld({ roles: ROLES, providers: () => providers, repos: () => world.repos, serviceAccounts })

function signedInUser(): string | null {
  try {
    return localStorage.getItem(SESSION_KEY)
  } catch {
    return null
  }
}

/** The mock session: a seeded user (sign in as `helpdesk` to see a user-admin who is not an administrator) or, for
 * any other name, an administrator. */
function session(username: string): Session {
  const known = admin.users.find((u) => u.username === username)
  return {
    user: known
      ? { id: known.id, username, display_name: known.display_name, email: known.email ?? null, source: known.source }
      : { id: ADMIN_USER_ID, username, display_name: username === 'admin' ? 'Administrator' : username, email: `${username}@example.com`, source: 'local' },
    csrf_token: CSRF,
    capabilities: admin.capabilities(username),
    preferences,
    expires_at: new Date(Date.now() + 12 * 3_600_000).toISOString(),
    must_change_password: false,
  }
}

function problem(status: number, title: string, detail?: string, errors?: FieldError[]) {
  const body: Problem = { type: 'about:blank', title, status, ...(detail ? { detail } : {}), ...(errors ? { errors } : {}) }
  return HttpResponse.json(body, { status, headers: { 'Content-Type': 'application/problem+json' } })
}

const notFound = () => problem(404, 'Not Found', 'The requested object does not exist or is not visible to you.')

function paginate<T>(items: T[], url: URL): { items: T[]; next_cursor: string | null } {
  const limit = Math.min(500, Math.max(1, Number(url.searchParams.get('limit') ?? 50)))
  const offset = Number(atob(url.searchParams.get('cursor') ?? '') || 0)
  const page = items.slice(offset, offset + limit)
  const next = offset + limit < items.length ? btoa(String(offset + limit)) : null
  return { items: page, next_cursor: next }
}

/** Unauthenticated → 401; unsafe method without the CSRF header → 403. */
function guard(request: Request) {
  if (!signedInUser()) return problem(401, 'Unauthorized', 'Sign in to continue.')
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method) && request.headers.get('X-CSRF-Token') !== CSRF) {
    return problem(403, 'Forbidden', 'Missing or invalid CSRF token.')
  }
  return null
}

const latency = () => delay(60 + Math.random() * 160)

function findRepo(name: string): Repository | undefined {
  return world.repos.find((r) => r.name === name)
}

function isPrivateHost(host: string): boolean {
  return /^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|\[?::1\]?|\[?f[cd])/i.test(host)
}

function validate(body: RepositoryWrite, existing?: Repository): FieldError[] {
  const errors: FieldError[] = []
  if (!existing) {
    if (!NAME_RE.test(body.name ?? '')) errors.push({ field: 'name', message: 'Use 2-63 letters, digits, dots, dashes or underscores, starting with a letter or digit.', code: 'pattern' })
    else if (world.repos.some((r) => r.name.toLowerCase() === body.name.toLowerCase())) errors.push({ field: 'name', message: 'A repository with this name already exists.', code: 'taken' })
    if (!(M0_FORMATS as readonly string[]).includes(body.format)) errors.push({ field: 'format', message: 'This format is not available yet.', code: 'unsupported' })
    if (body.format === 'go' && body.type === 'hosted') {
      errors.push({ field: 'type', message: 'go supports: proxy, group', code: 'unsupported' })
    } else if (body.type !== 'proxy' && !['docker', 'raw', 'maven', 'pypi', 'go'].includes(body.format)) {
      errors.push({ field: 'type', message: `${body.format} supports: proxy`, code: 'unsupported' })
    }
  }
  const quota = body.storage?.quota_bytes
  if (quota !== null && quota !== undefined) {
    if ((existing?.type ?? body.type) === 'group') errors.push({ field: 'storage.quota_bytes', message: 'groups store nothing: set quotas on their members', code: 'invalid' })
    else if (!Number.isInteger(quota) || quota < 1) errors.push({ field: 'storage.quota_bytes', message: 'must be a whole number of bytes, at least 1', code: 'range' })
  }
  if (body.type === 'group') {
    const members = body.group?.members ?? []
    if (!members.length) errors.push({ field: 'group.members', message: 'Pick at least one member.', code: 'required' })
    members.forEach((name, i) => {
      const member = findRepo(name)
      if (!member) errors.push({ field: `group.members.${i}`, message: `there is no repository '${name}'`, code: 'not_found' })
      else if (member.format !== body.format) errors.push({ field: `group.members.${i}`, message: `${name} is a ${member.format} repository`, code: 'format' })
      else if (member.type === 'group') errors.push({ field: `group.members.${i}`, message: 'groups cannot contain groups', code: 'invalid' })
    })
    const writable = body.group?.writable_member
    if (writable && (!members.includes(writable) || findRepo(writable)?.type !== 'hosted')) {
      errors.push({ field: 'group.writable_member', message: 'must be a hosted member', code: 'invalid' })
    }
    return errors
  }
  if (body.type === 'hosted') {
    if (body.proxy) errors.push({ field: 'proxy', message: 'only proxy repositories have an upstream', code: 'invalid' })
    ;(body.hosted?.immutable_tag_patterns ?? []).forEach((pattern, i) => {
      try {
        new RegExp(pattern)
      } catch {
        errors.push({ field: `hosted.immutable_tag_patterns.${i}`, message: `not a regular expression: ${pattern}`, code: 'format' })
      }
    })
    const opts = (body.format_options ?? {}) as { aliases?: string[]; docker_hub?: boolean }
    if (opts.aliases?.length) errors.push({ field: 'format_options.aliases', message: 'upstream aliases are for proxy repositories', code: 'invalid' })
    return errors
  }
  if (body.hosted) errors.push({ field: 'hosted', message: 'only hosted repositories have a write policy', code: 'invalid' })
  const url = body.proxy?.upstream_url?.trim() ?? ''
  if (!url) errors.push({ field: 'proxy.upstream_url', message: 'Enter the upstream URL.', code: 'required' })
  else {
    try {
      const parsed = new URL(url)
      if (!['http:', 'https:'].includes(parsed.protocol)) errors.push({ field: 'proxy.upstream_url', message: 'Use an http:// or https:// URL.', code: 'scheme' })
      else if (isPrivateHost(parsed.hostname) && !body.proxy?.allow_private_upstream) errors.push({ field: 'proxy.upstream_url', message: 'This address is in a private range. Enable "Allow private upstream" if that is intended.', code: 'ssrf' })
    } catch {
      errors.push({ field: 'proxy.upstream_url', message: 'Enter a valid URL.', code: 'url' })
    }
  }
  const auth = body.proxy?.auth
  if (auth && auth.kind !== 'none') {
    if (auth.kind === 'basic' && !auth.username) errors.push({ field: 'proxy.auth.username', message: 'Enter the upstream username.', code: 'required' })
    if (!auth.secret && !existing?.proxy?.auth?.secret_set) errors.push({ field: 'proxy.auth.secret', message: auth.kind === 'bearer' ? 'Enter the token.' : 'Enter the password or token.', code: 'required' })
  }
  const httpProxy = body.proxy?.http_proxy?.trim()
  if (httpProxy && !/^https?:\/\/[^/@?#\s]+\/?$/.test(httpProxy)) errors.push({ field: 'proxy.http_proxy', message: 'must be an http:// or https:// URL with a host and no credentials, e.g. http://proxy.example.com:3128', code: 'format' })
  const caPem = body.proxy?.ca_pem?.trim()
  if (caPem && !caPem.includes('-----BEGIN CERTIFICATE-----')) errors.push({ field: 'proxy.ca_pem', message: 'must contain at least one PEM certificate (-----BEGIN CERTIFICATE-----)', code: 'format' })
  const block = body.proxy?.auto_block
  if (block && block.max_s < block.initial_s) errors.push({ field: 'proxy.auto_block.max_s', message: 'must not be shorter than initial_s', code: 'range' })
  if (body.format === 'apt') {
    const dist = (body.format_options as { distribution?: string } | undefined)?.distribution
    if (!dist) errors.push({ field: 'format_options.distribution', message: 'Enter the default suite, e.g. trixie.', code: 'required' })
  }
  if (body.format === 'docker') {
    const aliases = (body.format_options as { aliases?: string[] } | undefined)?.aliases ?? []
    for (const alias of aliases) {
      const owner = world.repos.find((r) => r.name !== existing?.name && r.format === 'docker' && ((r.format_options as { aliases?: string[] } | undefined)?.aliases ?? []).includes(alias))
      if (!/^[a-z0-9.-]+(:\d+)?$/i.test(alias)) errors.push({ field: 'format_options.aliases', message: `"${alias}" is not a registry host name.`, code: 'pattern' })
      else if (owner) errors.push({ field: 'format_options.aliases', message: `"${alias}" is already an alias of ${owner.name}.`, code: 'taken' })
    }
  }
  return errors
}

function toRepository(body: RepositoryWrite, existing?: Repository): Repository {
  const now = new Date().toISOString()
  const secretSet = Boolean(body.proxy?.auth?.secret) || Boolean(existing?.proxy?.auth?.secret_set && body.proxy?.auth?.kind !== 'none')
  const proxy = body.proxy
    ? { ...body.proxy, auth: { kind: body.proxy.auth?.kind ?? 'none', username: body.proxy.auth?.username ?? null, secret_set: secretSet } }
    : undefined
  return {
    name: existing?.name ?? body.name,
    format: existing?.format ?? body.format,
    type: existing?.type ?? body.type,
    online: body.online,
    description: body.description ?? '',
    status: !body.online || body.proxy?.offline ? 'offline' : existing?.status === 'offline' ? 'ok' : (existing?.status ?? 'ok'),
    url: `${BASE_URL}/repository/${existing?.name ?? body.name}/`,
    proxy,
    hosted: (existing?.type ?? body.type) === 'hosted' ? { write_policy: body.hosted?.write_policy ?? 'allow_redeploy', immutable_tag_patterns: body.hosted?.immutable_tag_patterns ?? [] } : undefined,
    group: (existing?.type ?? body.type) === 'group' && body.group ? { members: body.group.members, writable_member: body.group.writable_member ?? null } : undefined,
    format_options: body.format_options,
    ...storageOf(body, existing),
    created_at: existing?.created_at ?? now,
    updated_at: now,
    stats: existing?.stats ?? { size_bytes: 0, artifact_count: 0, hit_ratio_24h: null, requests_24h: 0, bytes_served_24h: 0, bytes_saved_24h: 0, requests_spark_24h: Array(24).fill(0), last_activity_at: null },
  }
}

/** The quota fields of a written repository (groups store nothing: no quota). */
function storageOf(body: RepositoryWrite, existing?: Repository): Pick<Repository, 'storage' | 'quota'> {
  const group = (existing?.type ?? body.type) === 'group'
  const storage = { quota_bytes: group ? null : (body.storage?.quota_bytes ?? null), quota_mode: body.storage?.quota_mode ?? 'soft' }
  return { storage, quota: quotaView(storage, existing?.stats.size_bytes ?? 0) }
}

/** The low-disk guard as the server reports it: the settings and the data volume's free space now. */
function guardView(): StorageGuard {
  const total = world.storageTotal
  const free = total - world.storageUsed - 312 * 1024 ** 3
  const floor = Math.max(world.guard.min_free_bytes, Math.floor((total * world.guard.min_free_percent) / 100))
  return { ...world.guard, floor_bytes: floor, free_bytes: free, total_bytes: total, low: free < floor, low_since: free < floor ? new Date().toISOString() : null }
}

function range(url: URL): StatsRange {
  const r = url.searchParams.get('range') as StatsRange | null
  return r && r in RANGES ? r : '24h'
}

function repoGuard(name: string | readonly string[] | undefined, format?: Format) {
  const repo = typeof name === 'string' ? findRepo(name) : undefined
  if (!repo || (format && repo.format !== format)) return { repo: null, error: notFound() }
  return { repo, error: null }
}

export const handlers = [
  http.get(`${API}/config`, async () => {
    await latency()
    return HttpResponse.json({ instance_name: admin.instanceName, base_url: BASE_URL, version: '0.1.0-m0 (mock)', docker: { registry_host: REGISTRY_HOST, default_repo: world.repos.find((r) => (r.format_options as { default_repo?: boolean } | undefined)?.default_repo)?.name ?? null } })
  }),
  http.get(`${API}/auth/providers`, () =>
    HttpResponse.json([
      { id: 'local', kind: 'local', name: 'Waystation account' },
      ...providers
        .filter((p) => p.enabled)
        .sort((a, b) => a.order - b.order)
        // No real identity provider in mock mode: the OIDC button lands on the sign-in error banner.
        .map((p) => ({ id: p.id, kind: p.kind, name: p.name, login_url: p.kind === 'oidc' ? `${import.meta.env.BASE_URL}sign-in?error=oidc_unavailable` : null })),
    ]),
  ),
  http.post(`${API}/auth/login`, async ({ request }) => {
    await delay(350)
    const body = (await request.json()) as { username?: string; password?: string; provider?: string }
    const knownProvider = !body.provider || body.provider === 'local' || providers.some((p) => p.id === body.provider && p.enabled)
    if (!body.username || !body.password || body.password === 'wrong' || !knownProvider) {
      recordAudit({ action: 'auth.login', outcome: 'failure', reason: 'bad_credentials', actor: { kind: 'anonymous', id: null, name: 'anonymous', auth: 'none', provider: null, token_id: null }, target: { kind: 'user', id: null, name: body.username ?? null }, details: { username: body.username ?? '', via: 'ui' } })
      return problem(401, 'Unauthorized', 'Invalid username or password.')
    }
    localStorage.setItem(SESSION_KEY, body.username)
    recordAudit({ action: 'auth.login', outcome: 'success', actor: { kind: 'user', id: session(body.username).user.id, name: body.username, auth: 'session', provider: 'local', token_id: null }, target: { kind: 'user', id: session(body.username).user.id, name: body.username }, details: { username: body.username, via: 'ui' } })
    world.addActivity({ kind: 'auth', summary: `${body.username} signed in`, outcome: 'info', repository: null, format: null, artifact: null, principal: body.username, cache: null, bytes: null, duration_ms: null })
    return HttpResponse.json(session(body.username))
  }),
  http.post(`${API}/auth/logout`, () => {
    localStorage.removeItem(SESSION_KEY)
    return HttpResponse.json({ end_session_url: null })
  }),
  http.get(`${API}/auth/me`, async () => {
    await delay(40)
    const user = signedInUser()
    return user ? HttpResponse.json(session(user)) : problem(401, 'Unauthorized', 'Sign in to continue.')
  }),
  http.post(`${API}/auth/me/password`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    const body = (await request.json()) as PasswordChange
    if (body.new_password.length < 12) {
      return problem(422, 'Unprocessable Content', 'The request has invalid fields.', [
        { field: 'new_password', message: 'at least 12 characters', code: 'password' },
      ])
    }
    await latency()
    return new HttpResponse(null, { status: 204 })
  }),
  http.put(`${API}/auth/me/preferences`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    preferences = (await request.json()) as Preferences
    return HttpResponse.json(preferences)
  }),

  // ---------------------------------------------------------------- API tokens and service accounts
  ...tokenHandlers({ guard, problem, notFound, latency, currentUser: signedInUser }),

  // ---------------------------------------------------------------- cleanup policies and blob GC
  ...cleanupHandlers({ guard, problem, notFound, latency, currentUser: signedInUser }),

  // ---------------------------------------------------------------- users, groups, roles, settings, system (T-U08)
  ...adminHandlers({ guard, problem, notFound, latency, currentUser: signedInUser, storage: () => ({ total: world.storageTotal, used: world.storageUsed + 312 * 1024 ** 3 }) }, admin),

  // ---------------------------------------------------------------- tasks and workers (T-O04)
  ...tasksHandlers({ guard, problem, notFound, latency, currentUser: signedInUser }),

  // ---------------------------------------------------------------- import from Nexus (T-M01)
  ...nexusImportHandlers({ guard, problem, notFound, latency, currentUser: signedInUser }, (body) => toRepository(body)),

  // ---------------------------------------------------------------- audit log (T-S10)
  ...auditHandlers({ guard, problem, notFound, latency, currentUser: signedInUser }),

  // ---------------------------------------------------------------- repositories
  http.get(`${API}/repositories`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const url = new URL(request.url)
    const q = url.searchParams.get('q')?.toLowerCase()
    const f = url.searchParams.get('format')
    const t = url.searchParams.get('type')
    const s = url.searchParams.get('status')
    const items = world.repos
      .filter((r) => (!f || r.format === f) && (!t || r.type === t) && (!s || r.status === s))
      .filter((r) => !q || r.name.toLowerCase().includes(q) || (r.description ?? '').toLowerCase().includes(q))
      .sort((a, b) => a.name.localeCompare(b.name))
    return HttpResponse.json({ ...paginate(items, url), total: items.length })
  }),
  http.post(`${API}/repositories`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await delay(300)
    const body = (await request.json()) as RepositoryWrite
    const errors = validate(body)
    if (errors.some((e) => e.code === 'taken' && e.field === 'name')) return problem(409, 'Conflict', 'A repository with this name already exists.', errors)
    if (errors.length) return problem(422, 'Validation failed', 'Some fields need attention.', errors)
    const repo = toRepository(body)
    if ((body.format_options as { default_repo?: boolean } | undefined)?.default_repo) {
      for (const r of world.repos) if (r.format === 'docker') (r.format_options as { default_repo?: boolean }).default_repo = false
    }
    world.repos.push(repo)
    world.addActivity({ kind: 'repository', summary: `Repository ${repo.name} created`, outcome: 'info', repository: repo.name, format: repo.format, artifact: null, principal: signedInUser(), cache: null, bytes: null, duration_ms: null })
    return HttpResponse.json(repo, { status: 201, headers: { Location: `${API}/repositories/${repo.name}` } })
  }),
  http.post(`${API}/repositories/test-upstream`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    const body = (await request.json()) as RepositoryWrite
    await delay(700 + Math.random() * 600)
    const errors = validate({ ...body, name: 'probe-name', type: 'proxy', online: true } as RepositoryWrite).filter((e) => e.field.startsWith('proxy.upstream_url'))
    if (errors.length) return problem(422, 'Validation failed', 'Some fields need attention.', errors)
    const url = new URL(body.proxy!.upstream_url)
    const unreachable = /invalid|example\.(com|net|org)$/.test(url.hostname)
    const latencyMs = Math.round(40 + Math.random() * 180)
    const hub = url.hostname.includes('docker.io')
    return HttpResponse.json({
      reachable: !unreachable,
      http_status: unreachable ? null : hub ? 401 : 200,
      latency_ms: unreachable ? null : latencyMs,
      auth: unreachable ? undefined : body.proxy?.auth?.kind && body.proxy.auth.kind !== 'none' ? 'authenticated' : hub ? 'anonymous' : 'not_required',
      tls: url.protocol === 'https:' && !unreachable ? `TLSv1.3, CN=${url.hostname}` : null,
      checks: unreachable
        ? [{ name: 'dns', ok: false, message: `Could not resolve ${url.hostname}` }]
        : [
            { name: 'dns', ok: true, message: `${url.hostname} resolved to a public address` },
            { name: 'ssrf', ok: true, message: 'Address allowed' },
            ...(url.protocol === 'https:' ? [{ name: 'tls', ok: true, message: 'Certificate valid (TLSv1.3)' }] : []),
            { name: 'http', ok: true, message: `Responded in ${latencyMs} ms` },
            { name: 'protocol', ok: true, message: body.format === 'docker' ? 'OCI distribution API v2 detected' : body.format === 'npm' ? 'npm registry API detected' : body.format === 'apt' ? 'dists/ found' : 'HTTP file server' },
          ],
      rate_limit: hub ? { limit: 100, remaining: 100, window_s: 21600, reset_at: null, source: 'docker-hub' } : null,
    })
  }),
  http.get(`${API}/repositories/:name`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const repo = findRepo(String(params.name))
    return repo ? HttpResponse.json(repo) : notFound()
  }),
  http.put(`${API}/repositories/:name`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await delay(300)
    const existing = findRepo(String(params.name))
    if (!existing) return notFound()
    const body = (await request.json()) as RepositoryWrite
    const errors = validate(body, existing)
    if (errors.length) return problem(422, 'Validation failed', 'Some fields need attention.', errors)
    const updated = toRepository(body, existing)
    world.repos.splice(world.repos.indexOf(existing), 1, updated)
    world.addActivity({ kind: 'repository', summary: `Repository ${updated.name} updated`, outcome: 'info', repository: updated.name, format: updated.format, artifact: null, principal: signedInUser(), cache: null, bytes: null, duration_ms: null })
    return HttpResponse.json(updated)
  }),
  http.delete(`${API}/repositories/:name`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await delay(300)
    const existing = findRepo(String(params.name))
    if (!existing) return notFound()
    world.repos.splice(world.repos.indexOf(existing), 1)
    world.addActivity({ kind: 'repository', summary: `Repository ${existing.name} deleted`, outcome: 'info', repository: existing.name, format: existing.format, artifact: null, principal: signedInUser(), cache: null, bytes: null, duration_ms: null })
    return new HttpResponse(null, { status: 204 })
  }),
  http.get(`${API}/repositories/:name/setup`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const repo = findRepo(String(params.name))
    return repo ? HttpResponse.json(setupGuide(repo)) : notFound()
  }),
  http.get(`${API}/repositories/:name/stats/traffic`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const url = new URL(request.url)
    if (!findRepo(String(params.name))) return notFound()
    return HttpResponse.json(trafficSeries(world, range(url), url.searchParams.get('metric') === 'requests' ? 'requests' : 'bytes', 'cache', String(params.name)))
  }),

  // ---------------------------------------------------------------- browse
  http.get(`${API}/repositories/:name/browse`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name)
    if (!repo) return error
    const url = new URL(request.url)
    const path = url.searchParams.get('path') ?? ''
    const entries = repo.format === 'raw' ? rawListing(repo.name, path) : repo.format === 'maven' && path ? mavenListing(repo, path) : path ? [] : genericTree(repo.format)
    if (!entries) return notFound()
    const page = paginate(entries, url)
    return HttpResponse.json({ path, entries: page.items, next_cursor: page.next_cursor })
  }),
  http.get(`${API}/repositories/:name/docker/images`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'docker')
    if (!repo) return error
    const url = new URL(request.url)
    const q = url.searchParams.get('q')?.toLowerCase()
    const images = dockerImagesFor(repo.name).filter((i) => !q || i.name.includes(q))
    return HttpResponse.json(paginate(images, url))
  }),
  http.get(`${API}/repositories/:name/docker/tags`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'docker')
    if (!repo) return error
    const url = new URL(request.url)
    const image = url.searchParams.get('image') ?? ''
    const tags = dockerTagsFor(repo.name, image)
    if (!tags) return notFound()
    return HttpResponse.json({ image, ...paginate(tags, url) })
  }),
  http.get(`${API}/repositories/:name/docker/manifest`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'docker')
    if (!repo) return error
    const url = new URL(request.url)
    const manifest = dockerManifestFor(repo.name, url.searchParams.get('image') ?? '', url.searchParams.get('reference') ?? '', url.searchParams.get('platform') ?? undefined)
    return manifest ? HttpResponse.json(manifest) : notFound()
  }),
  http.get(`${API}/repositories/:name/npm/packages`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'npm')
    if (!repo) return error
    const url = new URL(request.url)
    const q = url.searchParams.get('q')?.toLowerCase()
    const items = npmPackageList().filter((p) => !q || p.name.includes(q)).sort((a, b) => a.name.localeCompare(b.name))
    return HttpResponse.json(paginate(items, url))
  }),
  http.get(`${API}/repositories/:name/npm/versions`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'npm')
    if (!repo) return error
    const url = new URL(request.url)
    const found = npmVersionsFor(url.searchParams.get('package') ?? '')
    if (!found) return notFound()
    return HttpResponse.json({ package: found.pkg, dist_tags: found.distTags, ...paginate(found.versions, url) })
  }),
  http.get(`${API}/repositories/:name/maven/artifacts`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'maven')
    if (!repo) return error
    const url = new URL(request.url)
    const q = url.searchParams.get('q')?.toLowerCase()
    const items = mavenArtifactsFor(repo).filter((a) => !q || `${a.group_id}:${a.artifact_id}`.toLowerCase().includes(q))
    return HttpResponse.json(paginate(items, url))
  }),
  http.get(`${API}/repositories/:name/maven/versions`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'maven')
    if (!repo) return error
    const url = new URL(request.url)
    const groupId = url.searchParams.get('group_id') ?? ''
    const artifactId = url.searchParams.get('artifact_id') ?? ''
    const versions = mavenVersionsFor(repo, groupId, artifactId)
    if (!versions) return notFound()
    return HttpResponse.json({ group_id: groupId, artifact_id: artifactId, ...paginate(versions, url) })
  }),
  http.get(`${API}/repositories/:name/pypi/projects`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'pypi')
    if (!repo || repo.type === 'group') return error ?? notFound()
    const url = new URL(request.url)
    const q = url.searchParams.get('q')?.toLowerCase().replace(/[-_.]+/g, '-')
    const items = pypiProjectList().filter((p) => !q || p.name.includes(q))
    return HttpResponse.json(paginate(items, url))
  }),
  http.get(`${API}/repositories/:name/pypi/versions`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'pypi')
    if (!repo || repo.type === 'group') return error ?? notFound()
    const url = new URL(request.url)
    const found = pypiVersionsFor(repo.name, repo.type === 'hosted', url.searchParams.get('project') ?? '')
    if (!found) return notFound()
    return HttpResponse.json({ project: found.project, ...paginate(found.versions, url) })
  }),
  http.post(`${API}/repositories/:name/pypi/yank`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'pypi')
    if (!repo || repo.type !== 'hosted') return error ?? notFound()
    const body = (await request.json()) as PypiYank
    if (!pypiYank(repo.name, body.project, body.version, body.reason ?? null, body.yanked)) return notFound()
    return HttpResponse.json({ project: body.project.toLowerCase(), version: body.version, yanked: body.yanked, files: 2 })
  }),
  http.get(`${API}/repositories/:name/go/modules`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'go')
    if (!repo || repo.type === 'group') return error ?? notFound()
    const url = new URL(request.url)
    const q = url.searchParams.get('q')?.toLowerCase()
    const items = goModuleList().filter((m) => !q || m.module.toLowerCase().includes(q))
    return HttpResponse.json(paginate(items, url))
  }),
  http.get(`${API}/repositories/:name/go/versions`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'go')
    if (!repo || repo.type === 'group') return error ?? notFound()
    const url = new URL(request.url)
    const found = goVersionsFor(repo.name, url.searchParams.get('module') ?? '')
    if (!found) return notFound()
    return HttpResponse.json({ module: found.module, ...paginate(found.versions, url) })
  }),
  http.get(`${API}/repositories/:name/apt/suites`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'apt')
    if (!repo) return error
    return HttpResponse.json(aptSuitesFor(repo.name))
  }),
  http.get(`${API}/repositories/:name/apt/packages`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const { repo, error } = repoGuard(params.name, 'apt')
    if (!repo) return error
    const url = new URL(request.url)
    const q = url.searchParams.get('q')?.toLowerCase()
    const cachedOnly = url.searchParams.get('cached') === 'true'
    const items = aptPackagesFor(repo.name, url.searchParams.get('suite') ?? '', url.searchParams.get('arch')).filter((p) => (!q || p.name.includes(q)) && (!cachedOnly || p.cached))
    return HttpResponse.json(paginate(items, url))
  }),

  // ---------------------------------------------------------------- search
  http.get(`${API}/search`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await delay(40 + Math.random() * 80)
    const url = new URL(request.url)
    const cursor = url.searchParams.get('cursor')
    const offset = cursor ? Number(atob(cursor)) : 0
    if (!Number.isInteger(offset) || offset < 0) return problem(400, 'Bad Request', 'invalid cursor')
    const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit') ?? 50)))
    const params: SearchParams = {
      q: url.searchParams.get('q') ?? '',
      formats: url.searchParams.getAll('format'),
      repositories: url.searchParams.getAll('repository'),
      kinds: url.searchParams.getAll('kind'),
      types: url.searchParams.getAll('type'),
      cached: url.searchParams.get('cached') === 'true',
    }
    return HttpResponse.json(mockSearch(world.repos, params, offset, limit, url.searchParams.get('facets') !== 'false'))
  }),

  // ---------------------------------------------------------------- stats & live
  http.get(`${API}/stats/summary`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    return HttpResponse.json(statsSummary(world, range(new URL(request.url))))
  }),
  http.get(`${API}/stats/traffic`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const url = new URL(request.url)
    return HttpResponse.json(trafficSeries(world, range(url), url.searchParams.get('metric') === 'requests' ? 'requests' : 'bytes', url.searchParams.get('split') === 'format' ? 'format' : 'cache', url.searchParams.get('repo')))
  }),
  http.get(`${API}/stats/storage`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const formats = new Map<Format, { bytes: number; artifacts: number }>()
    for (const r of world.repos) {
      const f = formats.get(r.format) ?? { bytes: 0, artifacts: 0 }
      f.bytes += r.stats.size_bytes
      f.artifacts += r.stats.artifact_count
      formats.set(r.format, f)
    }
    return HttpResponse.json({
      used_bytes: world.storageUsed,
      free_bytes: world.storageTotal - world.storageUsed - 312 * 1024 ** 3,
      total_bytes: world.storageTotal,
      blob_count: Math.round(world.artifacts * 0.86),
      dedup_saved_bytes: Math.round(world.storageUsed * 0.071),
      by_format: [...formats].map(([format, v]) => ({ format, ...v })).sort((a, b) => b.bytes - a.bytes),
      by_repository: world.repos.map((r) => ({ repository: r.name, format: r.format, bytes: r.stats.size_bytes, artifacts: r.stats.artifact_count })).sort((a, b) => b.bytes - a.bytes),
    })
  }),
  http.get(`${API}/storage/guard`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    return HttpResponse.json(guardView())
  }),
  http.put(`${API}/storage/guard`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await delay(300)
    const body = (await request.json()) as StorageGuardSettings
    const errors: FieldError[] = []
    if (!Number.isInteger(body.min_free_bytes) || body.min_free_bytes < 0) errors.push({ field: 'min_free_bytes', message: 'must be a whole number of bytes, 0 or more', code: 'range' })
    if (typeof body.min_free_percent !== 'number' || body.min_free_percent < 0 || body.min_free_percent > 90) errors.push({ field: 'min_free_percent', message: 'must be between 0 and 90', code: 'range' })
    if (errors.length) return problem(422, 'Validation failed', 'Some fields need attention.', errors)
    world.guard = { min_free_bytes: body.min_free_bytes, min_free_percent: body.min_free_percent, updated_at: new Date().toISOString() }
    return HttpResponse.json(guardView())
  }),
  http.get(`${API}/stats/upstreams`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    return HttpResponse.json(upstreamHealth(world))
  }),
  http.get(`${API}/transfers`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    return HttpResponse.json({ items: world.transfers(), as_of: new Date().toISOString() })
  }),
  http.get(`${API}/activity`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const url = new URL(request.url)
    const query = activityQuery(url)
    if (query.since && query.until && Date.parse(query.since) > Date.parse(query.until)) return problem(400, 'Validation failed', 'since must not be after until')
    return HttpResponse.json(activityPage(world.activity, url))
  }),
  http.get(`${API}/activity/export`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await delay(400)
    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace('T', '-').slice(0, 15)
    return new HttpResponse(activityCsv(world.activity, new URL(request.url)), {
      headers: { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': `attachment; filename="waystation-activity-${stamp}.csv"` },
    })
  }),

  // ---------------------------------------------------------------- administration: sign-in providers, roles
  http.get(`${API}/auth-providers`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    return HttpResponse.json([...providers].sort((a, b) => a.order - b.order || a.name.localeCompare(b.name)))
  }),
  http.post(`${API}/auth-providers`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const body = (await request.json()) as AuthProviderWrite
    const errors = validateProvider(body, providers)
    if (errors.length) return problem(422, 'Unprocessable Content', 'The request has invalid fields.', errors)
    const created = toConfig(body)
    providers.push(created)
    return HttpResponse.json(created, { status: 201 })
  }),
  http.post(`${API}/auth-providers/test-connection`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    const body = (await request.json()) as AuthProviderTestRequest
    const oidcBody = body.provider?.kind === 'oidc'
    if (oidcBody && !body.provider.oidc?.issuer?.trim()) return problem(422, 'Unprocessable Content', 'The request has invalid fields.', [{ field: 'provider.oidc.issuer', message: 'Enter the issuer URL.', code: 'required' }])
    if (!oidcBody && !body.provider?.ldap?.connection?.servers?.length) return problem(422, 'Unprocessable Content', 'The request has invalid fields.', [{ field: 'provider.ldap.connection.servers', message: 'Add at least one server.', code: 'required' }])
    await delay(400 + Math.random() * 500)
    return HttpResponse.json(testConnection(body.provider, providers.find((p) => p.id === body.id)))
  }),
  http.post(`${API}/auth-providers/test-user`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    const body = (await request.json()) as AuthProviderTestRequest
    if (!body.username?.trim()) return problem(422, 'Unprocessable Content', 'The request has invalid fields.', [{ field: 'username', message: 'Enter a username to look up.', code: 'required' }])
    if (body.provider?.kind !== 'ldap') return problem(422, 'Unprocessable Content', 'The request has invalid fields.', [{ field: 'provider.kind', message: 'OIDC providers are tested with a real sign-in', code: 'kind' }])
    await delay(300 + Math.random() * 400)
    return HttpResponse.json(testUser(body.provider, body.username, body.password))
  }),
  http.get(`${API}/auth-providers/:id`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    const found = providers.find((p) => p.id === params.id)
    return found ? HttpResponse.json(found) : notFound()
  }),
  http.put(`${API}/auth-providers/:id`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const index = providers.findIndex((p) => p.id === params.id)
    if (index < 0) return notFound()
    const body = (await request.json()) as AuthProviderWrite
    const errors = validateProvider(body, providers.filter((p) => p.id !== params.id), providers[index])
    if (errors.length) return problem(422, 'Unprocessable Content', 'The request has invalid fields.', errors)
    const updated = toConfig(body, providers[index])
    providers[index] = updated
    return HttpResponse.json(updated)
  }),
  http.delete(`${API}/auth-providers/:id`, async ({ request, params }) => {
    const denied = guard(request)
    if (denied) return denied
    await latency()
    const index = providers.findIndex((p) => p.id === params.id)
    if (index < 0) return notFound()
    providers.splice(index, 1)
    return new HttpResponse(null, { status: 204 })
  }),
]
