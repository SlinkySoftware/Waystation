// SPDX-License-Identifier: GPL-3.0-or-later
// Seeded audit log (SPEC §4.11) for mock mode: ~150 records over the last 30 days covering every action, with
// failures (reason codes) and diffs including a redacted secret. Example addresses only (RFC 5737, example.com).
import type { AuditActor, AuditChange, AuditRecord, AuditTarget } from '@/api/types'

import { between, pick, rng } from '../rng'

const DAY = 86_400_000
const ADMIN_ID = '01JC0000000000000000000ADM'
const LDAP_ID = '01JC0000000000000000000LDP'
const SA_ID = '01JC00000000000000000SA001'
const REPOS = ['dockerhub-proxy', 'ghcr-proxy', 'debian', 'debian-security', 'ubuntu', 'npm', 'rocky', 'epel', 'raw-releases']

type Actors = Record<'admin' | 'jdoe' | 'msmith' | 'ci' | 'anon' | 'system', AuditActor>
const ACTORS: Actors = {
  admin: { kind: 'user', id: ADMIN_ID, name: 'admin', auth: 'session', provider: 'local', token_id: null },
  jdoe: { kind: 'user', id: '01JC0000000000000000000JDO', name: 'jdoe', auth: 'session', provider: `ldap:${LDAP_ID}`, token_id: null },
  msmith: { kind: 'user', id: '01JC0000000000000000000MSM', name: 'msmith', auth: 'session', provider: `ldap:${LDAP_ID}`, token_id: null },
  ci: { kind: 'service_account', id: SA_ID, name: 'ci-runner', auth: 'api_token', provider: null, token_id: '01JC000000000000000TOK0042' },
  anon: { kind: 'anonymous', id: null, name: 'anonymous', auth: 'none', provider: null, token_id: null },
  system: { kind: 'system', id: null, name: 'system', auth: 'system', provider: null, token_id: null },
}

const IPS = ['192.0.2.14', '192.0.2.40', '198.51.100.23', '198.51.100.77', '203.0.113.5', '203.0.113.61']

interface Template {
  action: string
  weight: number
  make: (next: () => number) => Partial<AuditRecord> & { actor: AuditActor }
}

const target = (kind: string, name: string | null, id: string | null = null): AuditTarget => ({ kind, id, name })
const change = (field: string, before: unknown, after: unknown, redacted = false): AuditChange => ({ field, before, after, redacted })
const repoTarget = (next: () => number) => {
  const name = pick(REPOS, next)
  return target('repository', name, `01JCREPO${name.toUpperCase().replace(/[^A-Z]/g, '').padEnd(18, '0').slice(0, 18)}`)
}
const userTarget = (a: AuditActor) => target('user', a.name, a.id)

const TEMPLATES: Template[] = [
  { action: 'auth.login', weight: 30, make: (n) => { const a = pick([ACTORS.admin, ACTORS.jdoe, ACTORS.msmith], n); return { actor: a, target: userTarget(a), details: { username: a.name, via: n() < 0.8 ? 'ui' : 'docker' } } } },
  { action: 'auth.login', weight: 10, make: (n) => { const name = pick(['admin', 'jdoe', 'root', 'administrator', 'msmith'], n); const reason = pick(['bad_credentials', 'bad_credentials', 'unknown_user', 'locked', 'disabled', 'directory_unavailable', 'link_refused'], n); return { actor: ACTORS.anon, outcome: 'failure', reason, target: target('user', name), details: { username: name, via: n() < 0.7 ? 'ui' : 'docker' } } } },
  { action: 'auth.logout', weight: 10, make: (n) => { const a = pick([ACTORS.admin, ACTORS.jdoe], n); return { actor: a, target: userTarget(a) } } },
  { action: 'auth.lockout', weight: 1, make: () => ({ actor: ACTORS.anon, outcome: 'failure', reason: 'locked', target: target('user', 'msmith', ACTORS.msmith.id), details: { failures: 10, locked_minutes: 15 } }) },
  { action: 'auth.password_change', weight: 1, make: () => ({ actor: ACTORS.admin, target: userTarget(ACTORS.admin), changes: [change('password', null, null, true)] }) },
  { action: 'session.revoke', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('session', 'Firefox on Linux', '01JCSESSION000000000000001') }) },
  { action: 'token.create', weight: 4, make: (n) => ({ actor: pick([ACTORS.admin, ACTORS.jdoe], n), target: target('api_token', pick(['CI pipeline (build images)', 'Laptop docker login', 'Prometheus scraper'], n), '01JC000000000000000TOK0042'), details: { scopes: ['read', 'write'], repositories: ['dockerhub-proxy'], expires_at: null, owner: 'admin' } }) },
  { action: 'token.revoke', weight: 2, make: () => ({ actor: ACTORS.admin, target: target('api_token', 'Leaked in a log', '01JC000000000000000TOK0007'), details: { owner: 'admin' } }) },
  { action: 'token.use_refused', weight: 6, make: (n) => ({ actor: ACTORS.anon, outcome: 'failure', reason: 'invalid_token', target: null, details: { prefix: `wst_${pick(['Zk3q9xPa', 'r1Lw0bTe', 'Q8mvH2cs'], n)}` } }) },
  { action: 'service_account.create', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('service_account', 'ci-runner', SA_ID), details: { roles: ['deployer'] } }) },
  { action: 'service_account.update', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('service_account', 'ci-runner', SA_ID), changes: [change('roles', ['reader'], ['deployer']), change('description', '', 'Pipelines that build and push images')] }) },
  { action: 'service_account.delete', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('service_account', 'old-builder', '01JC00000000000000000SA000'), details: { revoked_tokens: 2 } }) },
  { action: 'user.create', weight: 2, make: () => ({ actor: ACTORS.system, target: target('user', 'msmith', ACTORS.msmith.id), details: { source: `ldap:${LDAP_ID}`, provisioned: true } }) },
  { action: 'user.link', weight: 1, make: () => ({ actor: ACTORS.system, target: target('user', 'jdoe', ACTORS.jdoe.id), details: { provider: `ldap:${LDAP_ID}`, subject: 'uid=jdoe,ou=people,dc=example,dc=com' } }) },
  { action: 'user.groups_sync', weight: 3, make: () => ({ actor: ACTORS.system, target: target('user', 'jdoe', ACTORS.jdoe.id), changes: [change('external_groups', ['cn=developers,ou=groups,dc=example,dc=com'], ['cn=developers,ou=groups,dc=example,dc=com', 'cn=release-managers,ou=groups,dc=example,dc=com'])] }) },
  { action: 'user.update', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('user', 'msmith', ACTORS.msmith.id), changes: [change('roles', ['reader'], ['reader', 'deployer']), change('disabled', true, false)] }) },
  { action: 'user.delete', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('user', 'contractor1', '01JC0000000000000000000CT1') }) },
  { action: 'group.create', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('group', 'release-managers', '01JCGROUP00000000000000RM1'), details: { roles: ['deployer'] } }) },
  { action: 'group.update', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('group', 'release-managers', '01JCGROUP00000000000000RM1'), changes: [change('roles', ['deployer'], ['deployer', 'viewer'])] }) },
  { action: 'group.delete', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('group', 'interns', '01JCGROUP00000000000000IN1') }) },
  { action: 'role.create', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('role', 'docker-pusher', 'docker-pusher'), details: { permissions: ['write:format:docker'] } }) },
  { action: 'role.update', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('role', 'docker-pusher', 'docker-pusher'), changes: [change('permissions', ['write:format:docker'], ['write:format:docker', 'delete:format:docker'])] }) },
  { action: 'role.delete', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('role', 'legacy-reader', 'legacy-reader') }) },
  { action: 'auth_provider.create', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('auth_provider', 'Corporate directory', LDAP_ID), details: { kind: 'ldap' } }) },
  { action: 'auth_provider.update', weight: 2, make: () => ({ actor: ACTORS.admin, target: target('auth_provider', 'Corporate directory', LDAP_ID), changes: [change('ldap.connection.servers', ['ldaps://ldap1.example.com'], ['ldaps://ldap1.example.com', 'ldaps://ldap2.example.com']), change('ldap.connection.bind_password', '***', '***', true), change('group_mapping', [{ external_group: 'cn=admins,ou=groups,dc=example,dc=com', roles: ['admin'] }], [{ external_group: 'cn=admins,ou=groups,dc=example,dc=com', roles: ['admin'] }, { external_group: 'cn=developers,ou=groups,dc=example,dc=com', roles: ['deployer'] }])] }) },
  { action: 'auth_provider.delete', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('auth_provider', 'Test directory', '01JC0000000000000000000LD2') }) },
  { action: 'auth_provider.test', weight: 2, make: (n) => ({ actor: ACTORS.admin, target: target('auth_provider', 'Corporate directory', LDAP_ID), details: { test: n() < 0.5 ? 'connection' : 'user', username: 'jdoe', ok: n() < 0.8 } }) },
  { action: 'repository.create', weight: 2, make: (n) => { const r = repoTarget(n); return { actor: ACTORS.admin, target: r, details: { format: r.name === 'npm' ? 'npm' : r.name?.includes('proxy') ? 'docker' : 'apt', type: 'proxy' } } } },
  { action: 'repository.update', weight: 6, make: (n) => ({ actor: pick([ACTORS.admin, ACTORS.jdoe], n), target: repoTarget(n), changes: [change('proxy.metadata_max_age_s', 1800, 3600), change('description', 'Docker Hub', 'Docker Hub (mirror)'), change('proxy.auth.secret', '***', '***', true)] }) },
  { action: 'repository.delete', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('repository', 'old-npm-mirror', '01JCREPOOLDNPM000000000001') }) },
  { action: 'repository.test_upstream', weight: 3, make: (n) => { const ok = n() < 0.75; return { actor: ACTORS.admin, target: target('upstream', 'registry-1.docker.io'), details: { upstream_url: 'https://registry-1.docker.io/', reachable: ok }, ...(ok ? {} : { outcome: 'failure' as const, reason: 'not_found' }) } } },
  { action: 'content.upload', weight: 2, make: () => ({ actor: ACTORS.ci, target: target('repository', 'raw-releases'), details: { path: 'tools/waystation-cli-1.4.2-linux-amd64.tar.gz', size: 18_402_311 } }) },
  { action: 'content.delete', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('repository', 'raw-releases'), details: { path: 'tools/waystation-cli-1.2.0-linux-amd64.tar.gz' } }) },
  { action: 'cleanup.run', weight: 3, make: (n) => ({ actor: ACTORS.system, target: repoTarget(n), details: { policy: 'Unused for 90 days', deleted_assets: Math.floor(between(3, 900, n)), reclaimed_bytes: Math.floor(between(1e6, 8e9, n)) } }) },
  { action: 'settings.update', weight: 1, make: () => ({ actor: ACTORS.admin, target: target('settings', 'security'), changes: [change('session_idle_minutes', 480, 240), change('audit_retention_days', 400, 730)] }) },
]

function requestId(next: () => number): string {
  let out = ''
  for (let i = 0; i < 26; i++) out += '0123456789ABCDEFGHJKMNPQRSTVWXYZ'[Math.floor(next() * 32)]
  return out
}

/** ~150 records, newest first: every template once, then weighted picks. */
export function seedAudit(now = Date.now(), count = 150): AuditRecord[] {
  const next = rng(4111)
  const total = TEMPLATES.reduce((s, x) => s + x.weight, 0)
  const chosen: Template[] = [...TEMPLATES]
  while (chosen.length < count) {
    let roll = next() * total
    const tpl = TEMPLATES.find((x) => (roll -= x.weight) < 0) ?? TEMPLATES[0]!
    chosen.push(tpl)
  }
  const records = chosen.map((tpl, i): AuditRecord => {
    const made = tpl.make(next)
    const system = made.actor.kind === 'system'
    return {
      id: `01JCAUDIT${String(i).padStart(17, '0')}`,
      ts: new Date(now - Math.floor(next() * 30 * DAY)).toISOString(),
      action: tpl.action,
      outcome: 'success',
      target: null,
      client_ip: system ? null : pick(IPS, next),
      request_id: system ? null : requestId(next),
      reason: null,
      details: {},
      changes: [],
      ...made,
    }
  })
  return records.sort((a, b) => b.ts.localeCompare(a.ts) || b.id.localeCompare(a.id))
}

/** Apply the `/audit` filters; null when a parameter is invalid (→ 422). */
export function filterAudit(records: AuditRecord[], query: URLSearchParams): AuditRecord[] | null {
  const from = query.get('from')
  const to = query.get('to')
  const fromMs = from ? Date.parse(from) : null
  const toMs = to ? Date.parse(to) : null
  if ((fromMs !== null && Number.isNaN(fromMs)) || (toMs !== null && Number.isNaN(toMs))) return null
  const outcome = query.get('outcome')
  if (outcome && outcome !== 'success' && outcome !== 'failure') return null
  const actor = query.get('actor')?.trim().toLowerCase()
  const targetQ = query.get('target')?.trim().toLowerCase()
  const actions = query.getAll('action').filter(Boolean)
  const matchesAction = (action: string) => actions.some((a) => (a.endsWith('.*') ? action.startsWith(a.slice(0, -1)) : action === a))
  return records.filter((r) => {
    const ts = Date.parse(r.ts)
    if (fromMs !== null && ts < fromMs) return false
    if (toMs !== null && ts >= toMs) return false
    if (outcome && r.outcome !== outcome) return false
    if (actor && r.actor.name.toLowerCase() !== actor && r.actor.id?.toLowerCase() !== actor) return false
    if (targetQ && r.target?.name?.toLowerCase() !== targetQ && r.target?.id?.toLowerCase() !== targetQ) return false
    if (actions.length && !matchesAction(r.action)) return false
    return true
  })
}

export function pageAudit(items: AuditRecord[], query: URLSearchParams): { items: AuditRecord[]; next_cursor: string | null } {
  const limit = Math.min(200, Math.max(1, Number(query.get('limit') ?? 50) || 50))
  const offset = Number(atob(query.get('cursor') ?? '') || 0) || 0
  const page = items.slice(offset, offset + limit)
  return { items: page, next_cursor: offset + limit < items.length ? btoa(String(offset + limit)) : null }
}

const CSV_COLUMNS = ['ts', 'action', 'outcome', 'reason', 'actor_kind', 'actor_name', 'actor_id', 'auth', 'provider', 'token_id', 'target_kind', 'target_name', 'target_id', 'client_ip', 'request_id', 'details', 'changes'] as const

function csvCell(value: unknown): string {
  let text = value === null || value === undefined ? '' : typeof value === 'string' ? value : JSON.stringify(value)
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

export function auditCsv(records: AuditRecord[]): string {
  const rows = records.map((r) =>
    [r.ts, r.action, r.outcome, r.reason, r.actor.kind, r.actor.name, r.actor.id, r.actor.auth, r.actor.provider, r.actor.token_id, r.target?.kind, r.target?.name, r.target?.id, r.client_ip, r.request_id, r.details, r.changes].map(csvCell).join(','),
  )
  return [CSV_COLUMNS.join(','), ...rows].join('\r\n') + '\r\n'
}

export function auditNdjson(records: AuditRecord[]): string {
  return records.map((r) => JSON.stringify(r)).join('\n') + (records.length ? '\n' : '')
}
