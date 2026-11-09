// SPDX-License-Identifier: GPL-3.0-or-later
// Seed data for the administration mocks (T-U08): users of every source and state, local groups, custom roles and
// the sessions of signed-in users. Names are fictional; addresses are RFC 5737; e-mail is example.com.
import type { Role, SessionInfo, UserDetail } from '@/api/types'

import { LDAP_ID, OIDC_ID } from './authProviders'

const MIN = 60_000
const HOUR = 3_600_000
const DAY = 86_400_000

export const ADMIN_USER_ID = '01JC0000000000000000000ADM'
const LDAP = `ldap:${LDAP_ID}`
const OIDC = `oidc:${OIDC_ID}`
const SUFFIX = 'cn=groups,cn=accounts,dc=example,dc=com'

export interface LocalGroupSeed {
  id: string
  name: string
  description: string
  roles: string[]
  created_at: string
  updated_at: string
}

export const CUSTOM_ROLES: Role[] = [
  {
    id: 'npm-publisher',
    name: 'npm publisher',
    description: 'Publish to the npm repositories and read everything else.',
    builtin: false,
    permissions: [
      { action: 'browse', scope: '*' },
      { action: 'read', scope: '*' },
      { action: 'write', scope: 'format:npm' },
      { action: 'delete', scope: 'format:npm' },
    ],
  },
  {
    id: 'security-auditor',
    name: 'Security auditor',
    description: 'Read the audit log and statistics; browse every repository.',
    builtin: false,
    permissions: [
      { action: 'browse', scope: '*' },
      { action: 'audit-read', scope: '*' },
      { action: 'stats-read', scope: '*' },
    ],
  },
  {
    id: 'user-manager',
    name: 'User manager',
    description: 'Manage users, groups and tokens (not administrators).',
    builtin: false,
    permissions: [
      { action: 'browse', scope: '*' },
      { action: 'read', scope: '*' },
      { action: 'user-admin', scope: '*' },
    ],
  },
  {
    id: 'docker-mirror',
    name: 'Docker mirror readers',
    description: 'Pull through the Docker proxies only.',
    builtin: false,
    permissions: [
      { action: 'browse', scope: 'format:docker' },
      { action: 'read', scope: 'format:docker' },
    ],
  },
]

export function seedLocalGroups(now = Date.now()): LocalGroupSeed[] {
  const at = (days: number) => new Date(now - days * DAY).toISOString()
  return [
    { id: '01JC00000000000000000GRP01', name: 'Release managers', description: 'Promote and publish releases.', roles: ['deployer'], created_at: at(120), updated_at: at(14) },
    { id: '01JC00000000000000000GRP02', name: 'Frontend team', description: 'Publishes the shared npm packages.', roles: ['npm-publisher'], created_at: at(90), updated_at: at(90) },
    { id: '01JC00000000000000000GRP03', name: 'Security', description: 'Reviews the audit log.', roles: ['security-auditor'], created_at: at(60), updated_at: at(5) },
    { id: '01JC00000000000000000GRP04', name: 'Contractors', description: 'Temporary read access.', roles: ['reader'], created_at: at(30), updated_at: at(30) },
  ]
}

const FIRST = ['amara', 'bruno', 'chen', 'dmitri', 'elena', 'farah', 'goran', 'hana', 'ivan', 'jonas', 'kai', 'lena', 'mateo', 'nadia', 'omar', 'priya', 'quinn', 'rosa', 'sven', 'tariq', 'uma', 'victor', 'wen', 'ximena', 'yusuf', 'zofia']
const LAST = ['adler', 'baker', 'costa', 'diaz', 'evans', 'fischer', 'garcia', 'haas']

function base(id: string, username: string, display: string, source: string, created: number, extra: Partial<UserDetail> = {}): UserDetail {
  return {
    id,
    username,
    display_name: display,
    email: `${username.replace(/@.*/, '')}@example.com`,
    source,
    disabled: false,
    locked: false,
    totp_enabled: false,
    roles: [],
    groups: [],
    last_login: null,
    created_at: new Date(created).toISOString(),
    locked_until: null,
    must_change_password: false,
    has_password: source === 'local',
    group_refs: [],
    external_groups: [],
    identities: [],
    effective_roles: [],
    ...extra,
  }
}

const iso = (ms: number) => new Date(ms).toISOString()

/** About 60 users: the named ones each show a state the UI handles, the rest fill the list for paging. */
export function seedUsers(now = Date.now()): UserDetail[] {
  const users: UserDetail[] = [
    base(ADMIN_USER_ID, 'admin', 'Administrator', 'local', now - 400 * DAY, { roles: ['admin'], last_login: iso(now - 5 * MIN) }),
    base('01JC0000000000000000000U01', 'alice', 'Alice Lindqvist', LDAP, now - 200 * DAY, {
      has_password: false,
      last_login: iso(now - 2 * HOUR),
      external_groups: [
        { provider: LDAP, id: `cn=developers,${SUFFIX}`, name: 'developers' },
        { provider: LDAP, id: `cn=ops,${SUFFIX}`, name: 'ops' },
      ],
      identities: [{ provider: LDAP, subject: 'uid=alice,cn=users,cn=accounts,dc=example,dc=com', linked_at: iso(now - 200 * DAY), last_login: iso(now - 2 * HOUR) }],
    }),
    base('01JC0000000000000000000U02', 'bob', 'Bob Okafor', OIDC, now - 150 * DAY, {
      has_password: false,
      last_login: iso(now - 26 * HOUR),
      external_groups: [{ provider: OIDC, id: '/platform', name: '/platform' }],
      identities: [{ provider: OIDC, subject: '5b0d7c4e-1f2a-4c55-9e1a-2f6d1f0c9a10', linked_at: iso(now - 150 * DAY), last_login: iso(now - 26 * HOUR) }],
    }),
    base('01JC0000000000000000000U03', 'dana', 'Dana Whitfield', 'local', now - 180 * DAY, { groups: ['01JC00000000000000000GRP01'], last_login: iso(now - 3 * DAY) }),
    base('01JC0000000000000000000U04', 'erin', 'Erin Moss', 'local', now - 300 * DAY, { disabled: true, roles: ['reader'], last_login: iso(now - 95 * DAY) }),
    base('01JC0000000000000000000U05', 'frank', 'Frank Novak', 'local', now - 100 * DAY, {
      locked: true,
      locked_until: iso(now + 12 * MIN),
      groups: ['01JC00000000000000000GRP02'],
      last_login: iso(now - 9 * DAY),
    }),
    base('01JC0000000000000000000U06', 'grace', 'Grace Hopper-Lee', 'local', now - 2 * DAY, { has_password: false, roles: ['viewer'] }),
    base('01JC0000000000000000000U07', 'helpdesk', 'Help desk', 'local', now - 50 * DAY, { roles: ['user-manager'], last_login: iso(now - 4 * HOUR) }),
    base('01JC0000000000000000000U08', 'ivy', 'Ivy Chen', 'local', now - 1 * DAY, { must_change_password: true, groups: ['01JC00000000000000000GRP04'] }),
    base('01JC0000000000000000000U09', 'sam', 'Sam Reyes', 'local', now - 70 * DAY, { groups: ['01JC00000000000000000GRP03'], last_login: iso(now - 30 * MIN), totp_enabled: true }),
  ]
  let i = 0
  for (const first of FIRST) {
    for (const last of LAST.slice(0, 2)) {
      i++
      const username = `${first}.${last}`
      const sourceIdx = i % 5
      const source = sourceIdx === 0 ? OIDC : sourceIdx < 3 ? LDAP : 'local'
      const id = `01JC00000000000000000F${String(i).padStart(4, '0')}`
      const created = now - (20 + ((i * 37) % 300)) * DAY
      const lastLogin = i % 7 === 0 ? null : iso(now - ((i * 7919 * 60_000) % (40 * DAY)))
      const display = `${first[0]!.toUpperCase()}${first.slice(1)} ${last[0]!.toUpperCase()}${last.slice(1)}`
      users.push(
        base(id, username, display, source, created, {
          has_password: source === 'local',
          last_login: lastLogin,
          roles: i % 9 === 0 ? ['deployer'] : i % 4 === 0 ? ['reader'] : [],
          groups: source === 'local' && i % 3 === 0 ? ['01JC00000000000000000GRP04'] : [],
          external_groups: source === LDAP ? [{ provider: LDAP, id: `cn=developers,${SUFFIX}`, name: 'developers' }] : source === OIDC ? [{ provider: OIDC, id: 'auditors', name: 'auditors' }] : [],
          identities: source === 'local' ? [] : [{ provider: source, subject: source === LDAP ? `uid=${username},cn=users,cn=accounts,dc=example,dc=com` : `sub-${id.slice(-6).toLowerCase()}`, linked_at: iso(created), last_login: lastLogin }],
        }),
      )
    }
  }
  return users
}

const BROWSERS = [
  'Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 15_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Safari/605.1.15',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.7444.60 Safari/537.36',
]

/** One to three sessions for users who signed in during the last week. */
export function seedSessions(users: UserDetail[], now = Date.now()): Map<string, SessionInfo[]> {
  const out = new Map<string, SessionInfo[]>()
  users.forEach((u, i) => {
    if (!u.last_login || u.disabled || now - Date.parse(u.last_login) > 7 * DAY) return
    const count = 1 + (i % 3)
    const list: SessionInfo[] = []
    for (let n = 0; n < count; n++) {
      const created = Date.parse(u.last_login) - n * 20 * HOUR
      list.push({
        id: `s${u.id.slice(-6).toLowerCase()}${n}`,
        current: false,
        created_at: iso(created),
        last_seen_at: iso(Math.min(now, created + (n === 0 ? 3 * MIN : 2 * HOUR))),
        expires_at: iso(created + 7 * DAY),
        ip: `198.51.100.${10 + ((i * 3 + n) % 200)}`,
        user_agent: BROWSERS[(i + n) % BROWSERS.length]!,
        provider_id: u.source.startsWith('oidc:') ? OIDC_ID : null,
      })
    }
    out.set(u.id, list)
  })
  return out
}
