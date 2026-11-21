// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Role } from '@/api/types'

import { rolesReaching } from './members'

const role = (id: string, permissions: Role['permissions']): Role => ({ id, name: id, description: '', builtin: false, permissions })
const npm = { name: 'npm-proxy', format: 'npm' as const, type: 'proxy' as const }

describe('repository members', () => {
  it('lists the roles that reach a repository with the actions they grant there', () => {
    const roles = [
      role('admin', [{ action: 'admin', scope: '*' }]),
      role('reader', [{ action: 'read', scope: '*' }]),
      role('npm-dev', [{ action: 'write', scope: 'format:npm' }]),
      role('docker-dev', [{ action: 'write', scope: 'docker-*' }]),
      role('stats', [{ action: 'stats-read', scope: '*' }]),
      role('pattern', [{ action: 'browse', scope: 'npm-*' }, { action: 'delete', scope: 'type:proxy' }]),
    ]
    const reach = rolesReaching(roles, npm)
    expect(reach.map((r) => r.role.id)).toEqual(['admin', 'npm-dev', 'pattern', 'reader'])
    expect(reach[0]).toMatchObject({ admin: true, actions: ['browse', 'read', 'write', 'delete', 'manage'] })
    expect(reach.find((r) => r.role.id === 'npm-dev')).toMatchObject({ actions: ['browse', 'read', 'write'], scopes: ['format:npm'] })
    expect(reach.find((r) => r.role.id === 'pattern')).toMatchObject({ actions: ['browse', 'read', 'delete'], scopes: ['npm-*', 'type:proxy'] })
    expect(reach.find((r) => r.role.id === 'reader')).toMatchObject({ actions: ['read'] })
  })
})
