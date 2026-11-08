// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { seedProviders } from '@/mocks/fixtures/authProviders'

import { applyPreset, emptyProvider, fromConfig, mappedRoles, serverHost, suffixOf, toWrite } from './providerForm'

describe('provider form model', () => {
  it('round-trips a stored provider without ever carrying the password', () => {
    const stored = seedProviders()[0]!
    const model = fromConfig(stored)
    expect(model.bindPasswordSet).toBe(true)
    expect(model.write.ldap.connection.bind_password).toBeNull()
    expect('bind_password_set' in model.write.ldap.connection).toBe(false)
    const body = toWrite(model)
    expect(body.ldap.connection.bind_password).toBeNull()
    expect(body.ldap.connection.servers).toEqual(stored.ldap.connection.servers)
    expect(body.group_mapping).toEqual(stored.group_mapping)
  })

  it('trims, drops blank servers and mapping rows, and turns empty optionals into null', () => {
    const model = emptyProvider()
    model.write.name = '  Directory  '
    model.write.ldap.connection.servers = [' ldap1.example.com ', '', '  ']
    model.write.ldap.connection.bind_dn = '   '
    model.write.ldap.connection.bind_password = ' secret with spaces '
    model.write.ldap.users.base_dn = ' dc=example,dc=com '
    model.write.ldap.groups.require_group = ''
    model.write.group_mapping = [
      { external_group: ' developers ', roles: ['deployer'] },
      { external_group: '', roles: [] },
    ]
    const body = toWrite(model)
    expect(body.name).toBe('Directory')
    expect(body.ldap.connection.servers).toEqual(['ldap1.example.com'])
    expect(body.ldap.connection.bind_dn).toBeNull()
    expect(body.ldap.connection.bind_password).toBe(' secret with spaces ')
    expect(body.ldap.users.base_dn).toBe('dc=example,dc=com')
    expect(body.ldap.groups.require_group).toBeNull()
    expect(body.group_mapping).toEqual([{ external_group: 'developers', roles: ['deployer'] }])
  })

  it('derives the directory suffix from the user base', () => {
    expect(suffixOf('cn=users,cn=accounts,dc=corp,dc=example,dc=com')).toBe('dc=corp,dc=example,dc=com')
    expect(suffixOf('')).toBe('dc=example,dc=com')
    expect(suffixOf('ou=people,o=org')).toBe('dc=example,dc=com')
  })
})

describe('directory presets', () => {
  it('fills FreeIPA bases and keeps servers and mappings', () => {
    const model = emptyProvider()
    model.write.ldap.connection.servers = ['ldap1.example.com']
    model.write.ldap.users.base_dn = 'dc=example,dc=com'
    model.write.group_mapping = [{ external_group: 'admins', roles: ['admin'] }]
    applyPreset(model, 'freeipa')
    const { users, groups, connection } = model.write.ldap
    expect(users.base_dn).toBe('cn=users,cn=accounts,dc=example,dc=com')
    expect(users.username_attribute).toBe('uid')
    expect(groups.mode).toBe('member_of')
    expect(groups.base_dn).toBe('cn=groups,cn=accounts,dc=example,dc=com')
    expect(connection.bind_dn).toBe('uid=waystation,cn=sysaccounts,cn=etc,dc=example,dc=com')
    expect(connection.servers).toEqual(['ldap1.example.com'])
    expect(model.write.group_mapping).toHaveLength(1)
  })

  it('fills Active Directory attributes, the disabled-account filter and nested groups', () => {
    const model = emptyProvider()
    model.write.ldap.connection.bind_dn = 'cn=svc,dc=example,dc=com'
    applyPreset(model, 'ad')
    const { users, groups, connection } = model.write.ldap
    expect(users.username_attribute).toBe('sAMAccountName')
    expect(users.display_name_attribute).toBe('displayName')
    expect(users.filter).toBe('(&(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))')
    expect(groups.filter).toBe('(objectClass=group)')
    expect(groups.nested).toBe(true)
    expect(connection.security).toBe('ldaps')
    expect(connection.bind_dn).toBe('cn=svc,dc=example,dc=com') // an existing bind DN is kept
  })

  it('uses group search for OpenLDAP', () => {
    const model = emptyProvider()
    applyPreset(model, 'openldap')
    expect(model.write.ldap.users.filter).toBe('(objectClass=inetOrgPerson)')
    expect(model.write.ldap.groups.mode).toBe('search')
    expect(model.write.ldap.groups.base_dn).toBe('ou=groups,dc=example,dc=com')
  })
})

describe('helpers', () => {
  it('maps groups to roles by DN or name, case-insensitively, without duplicates', () => {
    const mapping = [
      { external_group: 'CN=Developers,CN=Groups,DC=example,DC=com', roles: ['deployer'] },
      { external_group: 'ops', roles: ['viewer', 'deployer'] },
      { external_group: 'nobody', roles: ['admin'] },
    ]
    const groups = [
      { id: 'cn=developers,cn=groups,dc=example,dc=com', name: 'developers' },
      { id: 'cn=ops,cn=groups,dc=example,dc=com', name: 'OPS' },
    ]
    expect(mappedRoles(mapping, groups)).toEqual(['deployer', 'viewer'])
  })

  it('shows servers without the scheme', () => {
    expect(serverHost('ldaps://ldap1.example.com:636')).toBe('ldap1.example.com:636')
    expect(serverHost('ldap2.example.com')).toBe('ldap2.example.com')
  })
})
