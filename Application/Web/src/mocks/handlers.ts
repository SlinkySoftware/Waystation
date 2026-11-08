// SPDX-License-Identifier: GPL-3.0-or-later
// MSW handlers implementing the M0 contract (openapi/waystation-m0.yaml) against the mock world.
// Behaviour the UI depends on is modelled: problem+json errors with field errors, 404 for unknown objects,
// CSRF header on unsafe methods, cursor pagination, realistic latency.
import { delay, http, HttpResponse } from 'msw'

import type { FieldError, Format, Preferences, Problem, Repository, RepositoryWrite, Session, StatsRange } from '@/api/types'
import { M0_FORMATS } from '@/api/types'

import { activityCsv, activityPage, activityQuery } from './activity'
import { aptPackagesFor, aptSuitesFor } from './fixtures/apt'
import { dockerImagesFor, dockerManifestFor, dockerTagsFor } from './fixtures/docker'
import { npmPackageList, npmVersionsFor } from './fixtures/npm'
import { genericTree, rawListing } from './fixtures/raw'
import { BASE_URL, REGISTRY_HOST } from './fixtures/repositories'
import { setupGuide } from './setup'
import { RANGES, statsSummary, trafficSeries } from './stats'
import { tokenHandlers } from './tokens'
import { upstreamHealth, world } from './world'

const API = '/api/v1'
const SESSION_KEY = 'ws.mock.session'
const CSRF = 'mock-csrf-7f3a9c'
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$/

let preferences: Preferences = { theme: 'system', density: 'comfortable' }

function signedInUser(): string | null {
  try {
    return localStorage.getItem(SESSION_KEY)
  } catch {
    return null
  }
}

function session(username: string): Session {
  return {
    user: { id: '01JC0000000000000000000ADM', username, display_name: username === 'admin' ? 'Administrator' : username, email: `${username}@example.com`, source: 'local' },
    csrf_token: CSRF,
    capabilities: ['admin', 'repositories.create', 'repositories.manage', 'stats.read'],
    preferences,
    expires_at: new Date(Date.now() + 12 * 3_600_000).toISOString(),
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
    if (body.type !== 'proxy') errors.push({ field: 'type', message: 'Only proxy repositories are available in this build.', code: 'unsupported' })
  }
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
    format_options: body.format_options,
    created_at: existing?.created_at ?? now,
    updated_at: now,
    stats: existing?.stats ?? { size_bytes: 0, artifact_count: 0, hit_ratio_24h: null, requests_24h: 0, bytes_served_24h: 0, bytes_saved_24h: 0, requests_spark_24h: Array(24).fill(0), last_activity_at: null },
  }
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
    return HttpResponse.json({ instance_name: 'Waystation', base_url: BASE_URL, version: '0.1.0-m0 (mock)', docker: { registry_host: REGISTRY_HOST, default_repo: world.repos.find((r) => (r.format_options as { default_repo?: boolean } | undefined)?.default_repo)?.name ?? null } })
  }),
  http.get(`${API}/auth/providers`, () => HttpResponse.json([{ id: 'local', kind: 'local', name: 'Waystation account' }])),
  http.post(`${API}/auth/login`, async ({ request }) => {
    await delay(350)
    const body = (await request.json()) as { username?: string; password?: string }
    if (!body.username || !body.password || body.password === 'wrong') return problem(401, 'Unauthorized', 'Invalid username or password.')
    localStorage.setItem(SESSION_KEY, body.username)
    world.addActivity({ kind: 'auth', summary: `${body.username} signed in`, outcome: 'info', repository: null, format: null, artifact: null, principal: body.username, cache: null, bytes: null, duration_ms: null })
    return HttpResponse.json(session(body.username))
  }),
  http.post(`${API}/auth/logout`, () => {
    localStorage.removeItem(SESSION_KEY)
    return new HttpResponse(null, { status: 204 })
  }),
  http.get(`${API}/auth/me`, async () => {
    await delay(40)
    const user = signedInUser()
    return user ? HttpResponse.json(session(user)) : problem(401, 'Unauthorized', 'Sign in to continue.')
  }),
  http.put(`${API}/auth/me/preferences`, async ({ request }) => {
    const denied = guard(request)
    if (denied) return denied
    preferences = (await request.json()) as Preferences
    return HttpResponse.json(preferences)
  }),

  // ---------------------------------------------------------------- API tokens and service accounts
  ...tokenHandlers({ guard, problem, notFound, latency, currentUser: signedInUser }),

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
    const entries = repo.format === 'raw' ? rawListing(repo.name, path) : path ? [] : genericTree(repo.format)
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
]
