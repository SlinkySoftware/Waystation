// SPDX-License-Identifier: GPL-3.0-or-later
// Mock "Import from Nexus" (contract tag `import`): a fixed Nexus 3 repository set (the owner's repository names,
// public upstreams only) planned against the mock world. Most of it already exists in the demo, a few are new, some
// are unsupported. Password `wrong` answers like refused credentials; a URL without a host is a field error.
import { http, HttpResponse } from 'msw'

import type { NexusApplyRequest, NexusImportItem, NexusImportReport, NexusImportRequest, Repository, RepositoryWrite } from '@/api/types'

import type { MockHelpers } from './tokens'
import { world } from './world'

const API = '/api/v1'

interface NexusRepo {
  name: string
  format: string
  type: 'proxy' | 'hosted' | 'group'
  upstream?: string
  options?: Record<string, unknown>
  differences?: NexusImportItem['differences']
}

const AUTH_DIFF = { field: 'proxy.auth', nexus: 'username (nexus-reader)', waystation: 'none', note: 'Nexus does not return upstream passwords: set the credentials on the repository after the import' }
const CONNECTOR = { field: 'docker connector', nexus: 'httpPort 8082', waystation: '/v2/docker-group/', note: 'Nexus connectors are not imported: clients pull through <host>/<repository>/<image> or an upstream alias' }
const IMMUTABLE = (fmt: string) => ({ field: 'proxy.content_max_age_s', nexus: '1440 min', waystation: 'forever', note: `${fmt} content is verified by checksum and never changes, so Waystation keeps it until cleanup` })

const NEXUS: NexusRepo[] = [
  { name: 'dockerhub-proxy', format: 'docker', type: 'proxy', upstream: 'https://registry-1.docker.io', options: { aliases: ['docker.io'], docker_hub: true, default_repo: false }, differences: [IMMUTABLE('docker')] },
  { name: 'ghcr-proxy', format: 'docker', type: 'proxy', upstream: 'https://ghcr.io', options: { aliases: ['ghcr.io'], docker_hub: false, default_repo: false }, differences: [AUTH_DIFF] },
  { name: 'quay-proxy', format: 'docker', type: 'proxy', upstream: 'https://quay.io', options: { aliases: ['quay.io'], docker_hub: false, default_repo: false }, differences: [IMMUTABLE('docker')] },
  { name: 'docker-hosted', format: 'docker', type: 'hosted' },
  { name: 'docker-group', format: 'docker', type: 'group', differences: [CONNECTOR] },
  { name: 'debian', format: 'apt', type: 'proxy', upstream: 'http://deb.debian.org/debian/', options: { distribution: 'trixie', flat: false } },
  { name: 'debian-security', format: 'apt', type: 'proxy', upstream: 'http://security.debian.org/debian-security/', options: { distribution: 'trixie-security', flat: false } },
  { name: 'ubuntu', format: 'apt', type: 'proxy', upstream: 'http://archive.ubuntu.com/ubuntu/', options: { distribution: 'noble', flat: false } },
  { name: 'ubuntu-security', format: 'apt', type: 'proxy', upstream: 'http://security.ubuntu.com/ubuntu/', options: { distribution: 'noble-security', flat: false } },
  { name: 'ubuntu-ports', format: 'apt', type: 'proxy', upstream: 'http://ports.ubuntu.com/ubuntu-ports/', options: { distribution: 'noble', flat: false } },
  { name: 'raspberrypi', format: 'apt', type: 'proxy', upstream: 'http://archive.raspberrypi.com/debian/', options: { distribution: 'trixie', flat: false } },
  { name: 'rocky', format: 'yum', type: 'proxy', upstream: 'https://dl.rockylinux.org/pub/rocky/', options: { repo_path: '', gpg_key: null } },
  { name: 'almalinux', format: 'yum', type: 'proxy', upstream: 'https://repo.almalinux.org/almalinux/', options: { repo_path: '', gpg_key: null } },
  { name: 'epel', format: 'yum', type: 'proxy', upstream: 'https://dl.fedoraproject.org/pub/epel/', options: { repo_path: '', gpg_key: null } },
  { name: 'oracle', format: 'yum', type: 'proxy', upstream: 'https://yum.oracle.com/repo/OracleLinux/', options: { repo_path: '', gpg_key: null } },
  { name: 'npm', format: 'npm', type: 'proxy', upstream: 'https://registry.npmjs.org', options: { audit_passthrough: true } },
  { name: 'files', format: 'raw', type: 'hosted' },
  { name: 'maven-central', format: 'maven2', type: 'proxy' },
  { name: 'r-hosted', format: 'r', type: 'hosted' },
]

function plan(body: NexusImportRequest, applied: Set<string> | null, selected: string[] | null): NexusImportReport {
  const existing = new Map(world.repos.map((r) => [r.name.toLowerCase(), r]))
  const hasDefault = world.repos.find((r) => r.format === 'docker' && (r.format_options as { default_repo?: boolean } | undefined)?.default_repo)
  const items: NexusImportItem[] = NEXUS.map((n) => {
    const format = n.format === 'maven2' ? 'maven' : n.format === 'r' ? null : (n.format as NexusImportItem['format'])
    const base: Omit<NexusImportItem, 'action'> = { name: n.name, nexus_format: n.format, nexus_type: n.type, format, type: n.type, outcome: null, reason: null, repository: null, differences: n.differences ?? [], notes: [] }
    if (n.format === 'r') return { ...base, type: null, action: 'unsupported', reason: 'Waystation has no r format' }
    if (n.format === 'maven2') return { ...base, action: 'unsupported', reason: 'Waystation does not serve maven yet' }
    const found = existing.get(n.name.toLowerCase())
    if (n.format === 'docker' && n.type !== 'proxy') {
      const repository = {
        name: n.name,
        format: 'docker',
        type: n.type,
        online: true,
        description: `Imported from Nexus (docker ${n.type})`,
        ...(n.type === 'hosted'
          ? { hosted: { write_policy: 'allow_redeploy', immutable_tag_patterns: [] } }
          : { group: { members: ['docker-hosted', 'dockerhub-proxy', 'ghcr-proxy'], writable_member: 'docker-hosted' } }),
        format_options: { aliases: [], docker_hub: false, default_repo: false },
      } as RepositoryWrite
      if (found && !applied?.has(n.name)) return { ...base, action: 'exists', reason: 'already in Waystation; left unchanged', repository }
      return { ...base, action: 'create', repository }
    }
    if (n.type !== 'proxy') return { ...base, action: 'unsupported', reason: `Waystation has no ${n.format} ${n.type} repositories yet` }
    const options = { ...(n.options ?? {}) }
    if (!body.docker_aliases) delete options.aliases
    const repository: RepositoryWrite = {
      name: n.name,
      format: format as RepositoryWrite['format'],
      type: 'proxy',
      online: true,
      description: `Imported from Nexus (${n.format} proxy)`,
      proxy: {
        upstream_url: n.upstream ?? '',
        auth: { kind: 'none' },
        metadata_max_age_s: 86400,
        content_max_age_s: null,
        negative_ttl_s: 86400,
        stale_on_error: true,
        offline: false,
        allow_private_upstream: false,
      },
      format_options: options as RepositoryWrite['format_options'],
    }
    if (found && !applied?.has(n.name)) return { ...base, action: 'exists', reason: 'already in Waystation; left unchanged', repository }
    return { ...base, action: 'create', repository }
  })
  if (applied) {
    for (const item of items) {
      if (item.action !== 'create') item.outcome = item.action
      else item.outcome = applied.has(item.name) ? 'created' : selected && !selected.includes(item.name) ? 'not_selected' : 'exists'
    }
  }
  const count = (key: NexusImportItem['action']) => items.filter((i) => i.action === key).length
  return {
    applied: applied !== null,
    nexus_version: 'Nexus/3.70.1-02 (OSS)',
    items,
    summary: { total: items.length, create: count('create'), exists: count('exists'), unsupported: count('unsupported'), invalid: count('invalid'), created: applied?.size ?? 0, failed: 0 },
    default_docker_repo: hasDefault
      ? { name: hasDefault.name, reason: `Waystation already routes unprefixed names to ${hasDefault.name}; unchanged` }
      : { name: null, reason: 'not determinable (no Docker Hub proxy): choose one in the request' },
  }
}

function check(body: NexusImportRequest, h: MockHelpers): Response | null {
  let host = ''
  try {
    host = new URL(body.url).hostname
  } catch {
    host = ''
  }
  if (!host) return h.problem(422, 'Unprocessable Content', 'The request has invalid fields.', [{ field: 'url', message: 'must be an http(s) URL with a host', code: 'format' }])
  if (body.password === 'wrong') return h.problem(502, 'Bad Gateway', 'Nexus refused the credentials for the repository list (HTTP 401); an administrator account is needed')
  return null
}

export function nexusImportHandlers(h: MockHelpers, toRepository: (body: RepositoryWrite) => Repository) {
  return [
    http.post(`${API}/import/nexus/preview`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const body = (await request.json()) as NexusImportRequest
      return check(body, h) ?? HttpResponse.json(plan(body, null, null))
    }),
    http.post(`${API}/import/nexus/apply`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const body = (await request.json()) as NexusApplyRequest
      const refused = check(body, h)
      if (refused) return refused
      const before = plan(body, null, null)
      const created = new Set<string>()
      for (const item of before.items) {
        if (item.action !== 'create' || !item.repository) continue
        if (body.repositories && !body.repositories.includes(item.name)) continue
        world.repos.push(toRepository(item.repository))
        created.add(item.name)
      }
      return HttpResponse.json(plan(body, created, body.repositories ?? null))
    }),
  ]
}
