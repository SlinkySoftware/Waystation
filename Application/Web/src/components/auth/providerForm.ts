// SPDX-License-Identifier: GPL-3.0-or-later
// Provider form model (LDAP and OIDC): the API's AuthProviderWrite shape (so 422 field paths such as
// `ldap.connection.servers` or `oidc.issuer` map straight onto inputs), plus helpers for presets and for building the
// request body. Both kinds' settings are kept while editing; only the chosen kind's are sent.
import type { AuthProviderConfig, AuthProviderWrite, GroupMapping, LdapConfig, OidcConfig } from '@/api/types'

export type ProviderKind = AuthProviderWrite['kind']
export type SigningAlg = NonNullable<OidcConfig['signing_algs']>[number]
export type ClientAuth = OidcConfig['client_auth']

export const SIGNING_ALGS: SigningAlg[] = ['RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512', 'EdDSA', 'Ed25519']
export const CLIENT_AUTH_METHODS: ClientAuth[] = ['client_secret_basic', 'client_secret_post', 'none']

/** OIDC settings as edited: the read-only fields are kept on the model, not in here. */
export type OidcForm = Omit<OidcConfig, 'client_secret_set' | 'redirect_uri' | 'post_logout_redirect_uri' | 'require_group' | 'client_secret' | 'signing_algs'> & {
  require_group: string | null
  client_secret: string | null
  signing_algs: SigningAlg[]
}

export interface ProviderFormModel {
  write: Omit<AuthProviderWrite, 'ldap' | 'oidc' | 'group_mapping'> & { ldap: LdapConfig; oidc: OidcForm; group_mapping: GroupMapping[] }
  /** A bind password is stored on the server (edit): an empty password field keeps it. */
  bindPasswordSet: boolean
  /** An OIDC client secret is stored on the server (edit): an empty secret field keeps it. */
  clientSecretSet: boolean
  /** Saved OIDC providers: the URIs to register at the provider (known once the provider has an id). */
  redirectUri: string | null
  postLogoutRedirectUri: string | null
}

export type PresetId = 'freeipa' | 'ad' | 'openldap'

export const PRESET_IDS: PresetId[] = ['freeipa', 'ad', 'openldap']

export type OidcPresetId = 'keycloak' | 'authentik'

export const OIDC_PRESET_IDS: OidcPresetId[] = ['keycloak', 'authentik']

export const DEFAULT_SUFFIX = 'dc=example,dc=com'

export function emptyOidc(): OidcForm {
  return {
    issuer: '',
    client_id: '',
    client_secret: null,
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
  }
}

export function emptyProvider(kind: ProviderKind = 'ldap'): ProviderFormModel {
  return {
    bindPasswordSet: false,
    clientSecretSet: false,
    redirectUri: null,
    postLogoutRedirectUri: null,
    write: {
      kind,
      name: '',
      enabled: true,
      order: 100,
      oidc: emptyOidc(),
      ldap: {
        connection: { servers: [''], security: 'starttls', ca_pem: null, bind_dn: null, bind_password: null, connect_timeout_s: 5, timeout_s: 10 },
        users: { base_dn: '', filter: '(objectClass=person)', username_attribute: 'uid', display_name_attribute: 'cn', email_attribute: 'mail' },
        groups: {
          mode: 'member_of',
          base_dn: null,
          filter: '(objectClass=groupOfNames)',
          member_attribute: 'member',
          member_value: 'dn',
          name_attribute: 'cn',
          member_of_attribute: 'memberOf',
          nested: false,
          require_group: null,
        },
        cache_ttl_s: 60,
      },
      group_mapping: [],
    },
  }
}

/** Edit an existing provider: stored secrets are never sent back, only `bind_password_set` / `client_secret_set`. */
export function fromConfig(config: AuthProviderConfig): ProviderFormModel {
  const model = emptyProvider(config.kind)
  model.write.name = config.name
  model.write.enabled = config.enabled
  model.write.order = config.order
  model.write.group_mapping = config.group_mapping.map((m) => ({ external_group: m.external_group, roles: [...m.roles] }))
  if (config.ldap) {
    const { connection, users, groups, cache_ttl_s } = config.ldap
    const { bind_password_set: bindPasswordSet, ...conn } = connection
    model.bindPasswordSet = Boolean(bindPasswordSet)
    model.write.ldap = {
      connection: { ...conn, servers: conn.servers.length ? [...conn.servers] : [''], bind_password: null },
      users: { ...users },
      groups: { ...groups },
      cache_ttl_s,
    }
  }
  if (config.oidc) {
    const { client_secret_set: secretSet, redirect_uri: redirectUri, post_logout_redirect_uri: postLogout, ...oidc } = config.oidc
    model.clientSecretSet = Boolean(secretSet)
    model.redirectUri = redirectUri ?? null
    model.postLogoutRedirectUri = postLogout ?? null
    model.write.oidc = {
      ...emptyOidc(),
      ...oidc,
      scopes: [...oidc.scopes],
      require_group: oidc.require_group ?? null,
      signing_algs: oidc.signing_algs?.length ? [...oidc.signing_algs] : [...SIGNING_ALGS],
      client_secret: null,
    }
  }
  return model
}

/** Claim defaults for well-known OpenID Connect providers (issuer, client and secret are kept). */
export function applyOidcPreset(model: ProviderFormModel, preset: OidcPresetId): void {
  const oidc = model.write.oidc
  oidc.username_claim = 'preferred_username'
  oidc.display_name_claim = 'name'
  oidc.email_claim = 'email'
  oidc.groups_claim = 'groups'
  oidc.client_auth = 'client_secret_basic'
  // Keycloak sends `groups` through a group-membership mapper on the client; authentik's `profile` scope carries it.
  oidc.scopes = ['openid', 'profile', 'email']
  if (!oidc.issuer.trim()) oidc.issuer = preset === 'keycloak' ? 'https://sso.example.com/realms/example' : 'https://auth.example.com/application/o/waystation/'
}

/** The directory suffix (`dc=...` part) of the configured user base, used to fill preset base DNs. */
export function suffixOf(baseDn: string | null | undefined): string {
  const parts = (baseDn ?? '').split(',').map((p) => p.trim())
  const start = parts.findIndex((p) => /^dc=/i.test(p))
  return start >= 0 ? parts.slice(start).join(',') : DEFAULT_SUFFIX
}

/** Fill schema defaults for a directory type. Connection servers, names and mappings are kept. */
export function applyPreset(model: ProviderFormModel, preset: PresetId): void {
  const ldap = model.write.ldap
  const sfx = suffixOf(ldap.users.base_dn)
  if (preset === 'freeipa') {
    ldap.connection.security = 'starttls'
    ldap.connection.bind_dn ||= `uid=waystation,cn=sysaccounts,cn=etc,${sfx}`
    ldap.users = { base_dn: `cn=users,cn=accounts,${sfx}`, filter: '(objectClass=person)', username_attribute: 'uid', display_name_attribute: 'cn', email_attribute: 'mail' }
    // FreeIPA's memberOf already lists indirect memberships.
    ldap.groups = { ...ldap.groups, mode: 'member_of', base_dn: `cn=groups,cn=accounts,${sfx}`, filter: '(objectClass=groupOfNames)', member_attribute: 'member', member_value: 'dn', name_attribute: 'cn', member_of_attribute: 'memberOf', nested: false }
  } else if (preset === 'ad') {
    ldap.connection.security = 'ldaps'
    ldap.connection.bind_dn ||= `cn=waystation,cn=Users,${sfx}`
    ldap.users = {
      base_dn: sfx,
      filter: '(&(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))',
      username_attribute: 'sAMAccountName',
      display_name_attribute: 'displayName',
      email_attribute: 'mail',
    }
    ldap.groups = { ...ldap.groups, mode: 'member_of', base_dn: sfx, filter: '(objectClass=group)', member_attribute: 'member', member_value: 'dn', name_attribute: 'cn', member_of_attribute: 'memberOf', nested: true }
  } else {
    ldap.connection.security = 'starttls'
    ldap.users = { base_dn: `ou=people,${sfx}`, filter: '(objectClass=inetOrgPerson)', username_attribute: 'uid', display_name_attribute: 'cn', email_attribute: 'mail' }
    ldap.groups = { ...ldap.groups, mode: 'search', base_dn: `ou=groups,${sfx}`, filter: '(objectClass=groupOfNames)', member_attribute: 'member', member_value: 'dn', name_attribute: 'cn', member_of_attribute: 'memberOf', nested: false }
  }
}

const blankToNull = (v: string | null | undefined): string | null => (v && v.trim() ? v.trim() : null)

/** Request body: trimmed, empty optionals as null, blank servers and mapping rows dropped, password only when typed. */
export function toWrite(model: ProviderFormModel): AuthProviderWrite {
  const mapping: GroupMapping[] = model.write.group_mapping
    .map((m) => ({ external_group: m.external_group.trim(), roles: [...m.roles] }))
    .filter((m) => m.external_group || m.roles.length)
  const common = { name: model.write.name.trim(), enabled: model.write.enabled, order: Number(model.write.order), group_mapping: mapping }
  if (model.write.kind === 'oidc') return { kind: 'oidc', ...common, ldap: null, oidc: toOidcWrite(model) }
  return { kind: 'ldap', ...common, ldap: toLdapWrite(model), oidc: null }
}

function toOidcWrite(model: ProviderFormModel): OidcConfig {
  const oidc = model.write.oidc
  const scopes = oidc.scopes.map((s) => s.trim()).filter(Boolean)
  return {
    issuer: oidc.issuer.trim(),
    client_id: oidc.client_id.trim(),
    client_auth: oidc.client_auth,
    // Never trimmed; empty = keep the stored one; a public client sends none.
    client_secret: oidc.client_auth !== 'none' && oidc.client_secret ? oidc.client_secret : null,
    scopes: scopes.filter((s, i) => scopes.indexOf(s) === i),
    username_claim: oidc.username_claim.trim(),
    display_name_claim: oidc.display_name_claim.trim(),
    email_claim: oidc.email_claim.trim(),
    groups_claim: blankToNull(oidc.groups_claim),
    require_group: blankToNull(oidc.require_group),
    auto_provision: oidc.auto_provision,
    use_userinfo: oidc.use_userinfo,
    rp_logout: oidc.rp_logout,
    allow_private: oidc.allow_private,
    signing_algs: SIGNING_ALGS.filter((a) => oidc.signing_algs.includes(a)),
    timeout_s: Number(oidc.timeout_s),
  }
}

function toLdapWrite(model: ProviderFormModel): LdapConfig {
  const { connection, users, groups, cache_ttl_s } = model.write.ldap
  return {
    connection: {
      servers: connection.servers.map((s) => s.trim()).filter(Boolean),
      security: connection.security,
      ca_pem: blankToNull(connection.ca_pem),
      bind_dn: blankToNull(connection.bind_dn),
      // Never trimmed (spaces are legal in passwords); empty = keep the stored one.
      bind_password: connection.bind_password ? connection.bind_password : null,
      connect_timeout_s: Number(connection.connect_timeout_s),
      timeout_s: Number(connection.timeout_s),
    },
    users: {
      base_dn: users.base_dn.trim(),
      filter: users.filter.trim(),
      username_attribute: users.username_attribute.trim(),
      display_name_attribute: users.display_name_attribute.trim(),
      email_attribute: users.email_attribute.trim(),
    },
    groups: {
      mode: groups.mode,
      base_dn: blankToNull(groups.base_dn),
      filter: groups.filter.trim(),
      member_attribute: groups.member_attribute.trim(),
      member_value: groups.member_value,
      name_attribute: groups.name_attribute.trim(),
      member_of_attribute: groups.member_of_attribute.trim(),
      nested: groups.nested,
      require_group: blankToNull(groups.require_group),
    },
    cache_ttl_s: Number(cache_ttl_s),
  }
}

/** Roles a user with `groups` gets from `mapping` (DN or name, case-insensitive), in mapping order. */
export function mappedRoles(mapping: GroupMapping[], groups: { id: string; name: string }[]): string[] {
  const keys = new Set(groups.flatMap((g) => [g.id.toLowerCase(), g.name.toLowerCase()]))
  const roles: string[] = []
  for (const m of mapping) {
    if (keys.has(m.external_group.trim().toLowerCase())) for (const r of m.roles) if (!roles.includes(r)) roles.push(r)
  }
  return roles
}

/** Display host of a server entry (`ldaps://ldap1.example.com:636` → `ldap1.example.com:636`). */
export function serverHost(server: string): string {
  return server.replace(/^ldaps?:\/\//i, '').replace(/\/$/, '')
}
