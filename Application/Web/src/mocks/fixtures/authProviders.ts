// SPDX-License-Identifier: GPL-3.0-or-later
// Mock sign-in providers (T-A01, T-A02): a FreeIPA-style LDAP directory, a Keycloak OIDC provider, built-in roles,
// and the test-connection / test-discovery /
// test-user behaviour of the server (failover demo: the second server does not answer).
import type {
  AuthProviderConfig,
  AuthProviderConnectionTest,
  AuthProviderUserTest,
  AuthProviderWrite,
  ExternalGroup,
  FieldError,
  LdapServerTest,
  ProviderCheck,
  Role,
} from '@/api/types'
import { mappedRoles, serverHost, SIGNING_ALGS } from '@/components/auth/providerForm'

export const ROLES: Role[] = [
  {
    id: 'admin',
    name: 'Administrator',
    description: 'Everything, including users, settings and every repository.',
    builtin: true,
    permissions: [{ action: 'admin', scope: '*' }],
  },
  {
    id: 'reader',
    name: 'Reader',
    description: 'Browse and download from every repository.',
    builtin: true,
    permissions: [
      { action: 'browse', scope: '*' },
      { action: 'read', scope: '*' },
    ],
  },
  {
    id: 'deployer',
    name: 'Deployer',
    description: 'Browse, download and upload to every repository.',
    builtin: true,
    permissions: [
      { action: 'browse', scope: '*' },
      { action: 'read', scope: '*' },
      { action: 'write', scope: '*' },
    ],
  },
  {
    id: 'viewer',
    name: 'Viewer',
    description: 'Browse every repository and read statistics.',
    builtin: true,
    permissions: [
      { action: 'browse', scope: '*' },
      { action: 'stats-read', scope: '*' },
    ],
  },
]

const SUFFIX = 'dc=example,dc=com'
const GROUPS_BASE = `cn=groups,cn=accounts,${SUFFIX}`
const groupDn = (name: string, base = GROUPS_BASE) => `cn=${name},${base}`

export function seedProviders(): AuthProviderConfig[] {
  const created = new Date(Date.now() - 21 * 86_400_000).toISOString()
  return [
    {
      id: LDAP_ID,
      kind: 'ldap',
      name: 'Corporate directory',
      enabled: true,
      order: 10,
      ldap: {
        connection: {
          servers: ['ldaps://ldap1.example.com', 'ldaps://ldap2.example.com'],
          security: 'ldaps',
          ca_pem: null,
          bind_dn: `uid=waystation,cn=sysaccounts,cn=etc,${SUFFIX}`,
          bind_password_set: true,
          connect_timeout_s: 5,
          timeout_s: 10,
        },
        users: { base_dn: `cn=users,cn=accounts,${SUFFIX}`, filter: '(objectClass=person)', username_attribute: 'uid', display_name_attribute: 'cn', email_attribute: 'mail' },
        groups: {
          mode: 'member_of',
          base_dn: GROUPS_BASE,
          filter: '(objectClass=groupOfNames)',
          member_attribute: 'member',
          member_value: 'dn',
          name_attribute: 'cn',
          member_of_attribute: 'memberOf',
          nested: false,
          require_group: 'waystation-users',
        },
        cache_ttl_s: 60,
      },
      oidc: null,
      group_mapping: [
        { external_group: groupDn('admins'), roles: ['admin'] },
        { external_group: 'developers', roles: ['deployer'] },
        { external_group: 'ops', roles: ['viewer', 'reader'] },
      ],
      created_at: created,
      updated_at: created,
    },
    {
      id: OIDC_ID,
      kind: 'oidc',
      name: 'Keycloak',
      enabled: true,
      order: 20,
      ldap: null,
      oidc: {
        issuer: 'https://sso.example.com/realms/example',
        client_id: 'waystation',
        client_secret_set: true,
        client_auth: 'client_secret_basic',
        scopes: ['openid', 'profile', 'email'],
        username_claim: 'preferred_username',
        display_name_claim: 'name',
        email_claim: 'email',
        groups_claim: 'groups',
        require_group: null,
        auto_provision: true,
        use_userinfo: false,
        rp_logout: true,
        allow_private: false,
        signing_algs: [...SIGNING_ALGS],
        timeout_s: 10,
        redirect_uri: redirectUri(OIDC_ID),
        post_logout_redirect_uri: `${MOCK_BASE}/ui/sign-in`,
      },
      group_mapping: [
        { external_group: '/platform', roles: ['deployer'] },
        { external_group: 'auditors', roles: ['viewer'] },
      ],
      created_at: created,
      updated_at: created,
    },
  ]
}

export const OIDC_ID = '01JC0000000000000000000OID'
export const LDAP_ID = '01JC0000000000000000000LDP'
const MOCK_BASE = 'https://registry.example.com'
const redirectUri = (id: string) => `${MOCK_BASE}/auth/oidc/${id}/callback`

function validateOidc(body: AuthProviderWrite, existing: AuthProviderConfig | undefined, errors: FieldError[]): void {
  const oidc = body.oidc
  if (!oidc) {
    errors.push({ field: 'oidc', message: 'kind oidc needs it', code: 'required' })
    return
  }
  if (!/^https?:\/\/[A-Za-z0-9.-]+(:\d{1,5})?(\/.*)?$/.test(oidc.issuer)) errors.push({ field: 'oidc.issuer', message: 'must be an https URL with a host', code: 'format' })
  else if (oidc.issuer.startsWith('http:') && !oidc.allow_private) errors.push({ field: 'oidc.issuer', message: 'must be https (http is only allowed for private providers: allow private addresses)', code: 'format' })
  if (!oidc.client_id.trim()) errors.push({ field: 'oidc.client_id', message: 'Enter the client ID.', code: 'required' })
  if (oidc.client_auth !== 'none' && !oidc.client_secret && !existing?.oidc?.client_secret_set) errors.push({ field: 'oidc.client_secret', message: 'a confidential client needs its secret (or choose "none" for a public client)', code: 'required' })
  if (oidc.require_group && !oidc.groups_claim) errors.push({ field: 'oidc.require_group', message: 'a required group needs a groups claim', code: 'required' })
}

/** Directory entries the mock "LDAP server" knows. */
const PEOPLE: Record<string, { name: string; groups: string[]; locked?: boolean }> = {
  alice: { name: 'Alice Example', groups: ['developers', 'waystation-users'] },
  bob: { name: 'Bob Example', groups: ['ops', 'waystation-users'] },
  carol: { name: 'Carol Example', groups: ['waystation-users'], locked: true },
  dave: { name: 'Dave Example', groups: [] },
  admin: { name: 'Directory Administrator', groups: ['admins', 'waystation-users'] },
}
/** Groups that are members of other groups (nested): ops ⊂ platform. */
const PARENTS: Record<string, string[]> = { ops: ['platform'] }

const unreachable = (server: string) => /ldap2\.|down|192\.0\.2\./i.test(server)

export function validateProvider(body: AuthProviderWrite, others: AuthProviderConfig[], existing?: AuthProviderConfig): FieldError[] {
  const errors: FieldError[] = []
  const conn = body.ldap?.connection
  if (!body.name?.trim()) errors.push({ field: 'name', message: 'Enter a name.', code: 'required' })
  else if (others.some((p) => p.name.toLowerCase() === body.name.trim().toLowerCase())) errors.push({ field: 'name', message: 'Another provider already has this name.', code: 'taken' })
  if (body.kind === 'oidc') {
    validateOidc(body, existing, errors)
    validateMapping(body, errors)
    return errors
  }
  if (!conn?.servers?.length) errors.push({ field: 'ldap.connection.servers', message: 'Add at least one server.', code: 'required' })
  conn?.servers?.forEach((s, i) => {
    if (!/^(ldaps?:\/\/)?[A-Za-z0-9.-]+(:\d{1,5})?\/?$/.test(s)) errors.push({ field: `ldap.connection.servers.${i}`, message: 'Use host, host:port or an ldap:// / ldaps:// URL.', code: 'format' })
    else if (/^ldaps:/i.test(s) && conn.security !== 'ldaps') errors.push({ field: `ldap.connection.servers.${i}`, message: 'ldaps:// needs security "LDAPS".', code: 'scheme' })
  })
  if (conn?.ca_pem && !conn.ca_pem.includes('BEGIN CERTIFICATE')) errors.push({ field: 'ldap.connection.ca_pem', message: 'Paste one or more PEM certificates.', code: 'format' })
  if (!body.ldap?.users?.base_dn?.trim()) errors.push({ field: 'ldap.users.base_dn', message: 'Enter the base DN to search for users.', code: 'required' })
  for (const [field, value] of [['ldap.users.filter', body.ldap?.users?.filter], ['ldap.groups.filter', body.ldap?.groups?.filter]] as const) {
    if (value && !/^\(.*\)$/.test(value.trim())) errors.push({ field, message: 'An LDAP filter starts with "(" and ends with ")".', code: 'filter' })
  }
  if (body.ldap?.groups?.mode === 'search' && !body.ldap.groups.base_dn) errors.push({ field: 'ldap.groups.base_dn', message: 'Group search needs a base DN.', code: 'required' })
  validateMapping(body, errors)
  return errors
}

function validateMapping(body: AuthProviderWrite, errors: FieldError[]): void {
  body.group_mapping?.forEach((m, i) => {
    if (!m.external_group.trim()) errors.push({ field: `group_mapping.${i}.external_group`, message: 'Enter a group DN or name.', code: 'required' })
    const unknown = m.roles.filter((r) => !ROLES.some((role) => role.id === r))
    if (unknown.length) errors.push({ field: `group_mapping.${i}.roles`, message: `Unknown role: ${unknown.join(', ')}.`, code: 'unknown' })
    if (!m.roles.length) errors.push({ field: `group_mapping.${i}.roles`, message: 'Pick at least one role.', code: 'required' })
  })
}

export function toConfig(body: AuthProviderWrite, existing?: AuthProviderConfig): AuthProviderConfig {
  const now = new Date().toISOString()
  const id = existing?.id ?? `01JC${Math.random().toString(36).slice(2, 12).toUpperCase().padEnd(22, '0')}`
  const common = {
    id,
    name: body.name.trim(),
    enabled: body.enabled ?? true,
    order: body.order ?? 100,
    group_mapping: body.group_mapping ?? [],
    created_at: existing?.created_at ?? now,
    updated_at: now,
  }
  if (body.kind === 'oidc' && body.oidc) {
    const { client_secret: secret, ...oidc } = body.oidc
    const secretSet = oidc.client_auth !== 'none' && (Boolean(secret) || Boolean(existing?.oidc?.client_secret_set))
    return {
      ...common,
      kind: 'oidc',
      ldap: null,
      oidc: { ...oidc, client_secret_set: secretSet, redirect_uri: redirectUri(id), post_logout_redirect_uri: `${MOCK_BASE}/ui/sign-in` },
    }
  }
  const ldap = body.ldap!
  const { bind_password: password, ...conn } = ldap.connection
  return {
    ...common,
    kind: 'ldap',
    ldap: { ...ldap, connection: { ...conn, bind_password_set: Boolean(password) || Boolean(existing?.ldap?.connection.bind_password_set && conn.bind_dn) } },
    oidc: null,
  }
}

/** OIDC "test discovery": `down` / RFC 5737 issuers do not answer; the client check needs a secret. */
function testDiscovery(body: AuthProviderWrite, existing?: AuthProviderConfig): AuthProviderConnectionTest {
  const oidc = body.oidc!
  const issuer = oidc.issuer.replace(/\/$/, '')
  if (/down|192\.0\.2\./i.test(issuer)) {
    return { ok: false, servers: [{ server: oidc.issuer, ok: false, latency_ms: null, tls: null, checks: [{ name: 'discovery', ok: false, message: `${issuer} did not answer in ${oidc.timeout_s} s` }] }], discovery: null }
  }
  const secretKnown = oidc.client_auth === 'none' || Boolean(oidc.client_secret) || Boolean(existing?.oidc?.client_secret_set)
  const offered = ['RS256', 'ES256'] as const
  const common = offered.filter((a) => oidc.signing_algs?.includes(a))
  const checks: ProviderCheck[] = [
    { name: 'discovery', ok: true, message: `issuer ${oidc.issuer}` },
    { name: 'jwks', ok: common.length > 0, message: common.length ? `${common.length} usable signing key(s)` : 'no key fits the accepted algorithms' },
    { name: 'algorithms', ok: common.length > 0, message: common.length ? common.join(', ') : `the provider signs with ${offered.join(', ')}` },
    { name: 'pkce', ok: true, message: 'S256' },
  ]
  if (oidc.rp_logout) checks.push({ name: 'logout', ok: true, message: 'supported' })
  checks.push(secretKnown ? { name: 'client', ok: true, message: 'client credentials accepted' } : { name: 'client', ok: false, message: 'the provider refused the client (invalid_client)' })
  const ok = checks.every((c) => c.ok)
  return {
    ok,
    servers: [{ server: oidc.issuer, ok, latency_ms: 20 + Math.round(Math.random() * 40), tls: null, checks }],
    discovery: {
      issuer: oidc.issuer,
      authorization_endpoint: `${issuer}/protocol/openid-connect/auth`,
      token_endpoint: `${issuer}/protocol/openid-connect/token`,
      jwks_uri: `${issuer}/protocol/openid-connect/certs`,
      userinfo_endpoint: `${issuer}/protocol/openid-connect/userinfo`,
      end_session_endpoint: `${issuer}/protocol/openid-connect/logout`,
      signing_algs: [...offered],
      scopes_supported: ['openid', 'profile', 'email', 'groups'],
      keys: common.length,
      redirect_uri: redirectUri(existing?.id ?? 'new'),
      post_logout_redirect_uri: `${MOCK_BASE}/ui/sign-in`,
    },
  }
}

function serverTest(server: string, body: AuthProviderWrite, passwordKnown: boolean): LdapServerTest {
  const host = serverHost(server)
  const { connection, users, groups } = body.ldap!
  if (unreachable(server)) {
    return { server, ok: false, latency_ms: null, tls: null, checks: [{ name: 'connect', ok: false, message: `${host}: connection timed out after ${connection.connect_timeout_s} s` }] }
  }
  const checks: ProviderCheck[] = [{ name: 'connect', ok: true, message: `Connected to ${host}` }]
  const tls = connection.security === 'none' ? null : `TLSv1.3, CN=${host.split(':')[0]}`
  if (tls) checks.push({ name: 'tls', ok: true, message: `${connection.security === 'starttls' ? 'StartTLS' : 'LDAPS'}: ${tls}${connection.ca_pem ? ' (custom CA)' : ''}` })
  if (connection.bind_dn) {
    checks.push(passwordKnown ? { name: 'bind', ok: true, message: `Bound as ${connection.bind_dn}` } : { name: 'bind', ok: false, message: 'No bind password: enter one or save it first' })
  } else checks.push({ name: 'bind', ok: true, message: 'Anonymous search' })
  checks.push({ name: 'user_base', ok: true, message: `${users.base_dn} exists (${Object.keys(PEOPLE).length} users)` })
  if (groups.mode !== 'none' && groups.base_dn) checks.push({ name: 'group_base', ok: true, message: `${groups.base_dn} exists` })
  return { server, ok: checks.every((c) => c.ok), latency_ms: 3 + Math.round(Math.random() * 12), tls, checks }
}

export function testConnection(body: AuthProviderWrite, existing?: AuthProviderConfig): AuthProviderConnectionTest {
  if (body.kind === 'oidc') return testDiscovery(body, existing)
  const ldap = body.ldap!
  const passwordKnown = Boolean(ldap.connection.bind_password) || Boolean(existing?.ldap?.connection.bind_password_set)
  const servers = ldap.connection.servers.map((s) => serverTest(s, body, passwordKnown))
  return { ok: servers.every((s) => s.ok), servers, discovery: null }
}

function groupsOf(direct: string[], body: AuthProviderWrite): ExternalGroup[] {
  const base = body.ldap!.groups.base_dn || GROUPS_BASE
  const names = [...direct]
  if (body.ldap!.groups.nested) {
    for (let i = 0; i < names.length; i++) for (const p of PARENTS[names[i]!] ?? []) if (!names.includes(p)) names.push(p)
  }
  return names.map((n) => ({ id: groupDn(n, base), name: n }))
}

export function testUser(body: AuthProviderWrite, username: string, password: string | null | undefined): AuthProviderUserTest {
  const server = body.ldap!.connection.servers.find((s) => !unreachable(s)) ?? null
  const base: AuthProviderUserTest = { ok: false, found: false, server, user: null, groups: [], require_group_ok: null, password: 'not_tested', roles: [], checks: [] }
  if (!server) return { ...base, checks: [{ name: 'connect', ok: false, message: 'No server answered' }] }
  const key = username.trim().toLowerCase()
  const person = PEOPLE[key]
  const { users, groups: groupCfg } = body.ldap!
  const filter = `(&${users.filter}(${users.username_attribute}=${key}))`
  if (!person) return { ...base, checks: [{ name: 'search', ok: false, message: `No entry matches ${filter} under ${users.base_dn}` }] }
  const groups = groupCfg.mode === 'none' ? [] : groupsOf(person.groups, body)
  const checks: ProviderCheck[] = [{ name: 'search', ok: true, message: `Found 1 entry with ${filter}` }]
  checks.push({ name: 'groups', ok: true, message: groupCfg.mode === 'none' ? 'Group discovery is off' : `${groups.length} group(s) via ${groupCfg.mode === 'member_of' ? groupCfg.member_of_attribute : 'group search'}` })
  let requireOk: boolean | null = null
  const required = groupCfg.require_group?.trim().toLowerCase()
  if (required) {
    requireOk = groups.some((g) => g.id.toLowerCase() === required || g.name.toLowerCase() === required)
    checks.push({ name: 'require_group', ok: requireOk, message: requireOk ? `Member of ${groupCfg.require_group}` : `Not a member of ${groupCfg.require_group}: sign-in is refused` })
  }
  let pw: AuthProviderUserTest['password'] = 'not_tested'
  if (password) {
    pw = password === 'wrong' || person.locked ? 'failed' : 'ok'
    checks.push({ name: 'password', ok: pw === 'ok', message: pw === 'ok' ? 'Password accepted (bind as the user)' : person.locked ? 'Bind refused: the account is locked or disabled' : 'Bind refused: invalid credentials' })
  }
  return {
    ok: (requireOk ?? true) && pw !== 'failed',
    found: true,
    server,
    user: { dn: `uid=${key},${users.base_dn}`, username: key, display_name: person.name, email: `${key}@example.com` },
    groups,
    require_group_ok: requireOk,
    password: pw,
    roles: mappedRoles(body.group_mapping ?? [], groups),
    checks,
  }
}
