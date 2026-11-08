// SPDX-License-Identifier: GPL-3.0-or-later
// LDAP provider form model: the API's AuthProviderWrite shape (so 422 field paths such as `ldap.connection.servers`
// map straight onto inputs), plus helpers for presets and for building the request body.
import type { AuthProviderConfig, AuthProviderWrite, GroupMapping } from '@/api/types'

export interface ProviderFormModel {
  write: AuthProviderWrite & { group_mapping: GroupMapping[] }
  /** A bind password is stored on the server (edit): an empty password field keeps it. */
  bindPasswordSet: boolean
}

export type PresetId = 'freeipa' | 'ad' | 'openldap'

export const PRESET_IDS: PresetId[] = ['freeipa', 'ad', 'openldap']

export const DEFAULT_SUFFIX = 'dc=example,dc=com'

export function emptyProvider(): ProviderFormModel {
  return {
    bindPasswordSet: false,
    write: {
      kind: 'ldap',
      name: '',
      enabled: true,
      order: 100,
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

/** Edit an existing provider: the stored password is never sent back, only `bind_password_set`. */
export function fromConfig(config: AuthProviderConfig): ProviderFormModel {
  const { connection, users, groups, cache_ttl_s } = config.ldap
  const { bind_password_set: bindPasswordSet, ...conn } = connection
  return {
    bindPasswordSet: Boolean(bindPasswordSet),
    write: {
      kind: config.kind,
      name: config.name,
      enabled: config.enabled,
      order: config.order,
      ldap: {
        connection: { ...conn, servers: conn.servers.length ? [...conn.servers] : [''], bind_password: null },
        users: { ...users },
        groups: { ...groups },
        cache_ttl_s,
      },
      group_mapping: config.group_mapping.map((m) => ({ external_group: m.external_group, roles: [...m.roles] })),
    },
  }
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
  const { connection, users, groups, cache_ttl_s } = model.write.ldap
  const mapping: GroupMapping[] = model.write.group_mapping
    .map((m) => ({ external_group: m.external_group.trim(), roles: [...m.roles] }))
    .filter((m) => m.external_group || m.roles.length)
  return {
    kind: 'ldap',
    name: model.write.name.trim(),
    enabled: model.write.enabled,
    order: Number(model.write.order),
    ldap: {
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
    },
    group_mapping: mapping,
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
