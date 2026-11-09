// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { fromMatrix, impliedActions, parseSource, repositoryOnly, scopeKind, scopeMatches, scopeProblem, toMatrix } from './rbac'

const repo = (name: string, format = 'docker', type = 'proxy') => ({ name, format, type })

describe('scopes', () => {
  it('match like the server: *, names and globs case-insensitively, format: and type:', () => {
    expect(scopeMatches('*', repo('a'))).toBe(true)
    expect(scopeMatches('DockerHub-Proxy', repo('dockerhub-proxy'))).toBe(true)
    expect(scopeMatches('docker-*', repo('docker-hub'))).toBe(true)
    expect(scopeMatches('docker-*', repo('npm'))).toBe(false)
    expect(scopeMatches('npm-?', repo('npm-1'))).toBe(true)
    expect(scopeMatches('repo-[ab]', repo('repo-b'))).toBe(true)
    expect(scopeMatches('repo-[!ab]', repo('repo-b'))).toBe(false)
    expect(scopeMatches('a.b', repo('axb'))).toBe(false) // dots are literal
    expect(scopeMatches('format:npm', repo('x', 'npm'))).toBe(true)
    expect(scopeMatches('type:hosted', repo('x', 'npm', 'proxy'))).toBe(false)
  })

  it('classify and validate', () => {
    expect(scopeKind('*')).toBe('all')
    expect(scopeKind('format:apt')).toBe('format')
    expect(scopeKind('type:group')).toBe('type')
    expect(scopeKind('docker-*')).toBe('glob')
    expect(scopeKind('npm')).toBe('name')
    expect(scopeProblem('')).toBe('required')
    expect(scopeProblem('type:weird')).toBe('pattern')
    expect(scopeProblem('has space')).toBe('pattern')
    expect(scopeProblem('format:npm')).toBeNull()
  })
})

describe('permission matrix', () => {
  it('shows implied browse and read, keeps global actions apart, and round-trips', () => {
    const m = toMatrix([
      { action: 'write', scope: 'format:npm' },
      { action: 'browse', scope: '*' },
      { action: 'audit-read', scope: '*' },
    ])
    expect(m.rows).toEqual([
      { scope: 'format:npm', actions: ['browse', 'read', 'write'] },
      { scope: '*', actions: ['browse'] },
    ])
    expect(m.global).toEqual(['audit-read'])
    expect(fromMatrix(m)).toEqual([
      { action: 'browse', scope: 'format:npm' },
      { action: 'read', scope: 'format:npm' },
      { action: 'write', scope: 'format:npm' },
      { action: 'browse', scope: '*' },
      { action: 'audit-read', scope: '*' },
    ])
  })

  it('drops empty scopes and merges duplicate rows', () => {
    const perms = fromMatrix({ rows: [{ scope: ' npm ', actions: ['read'] }, { scope: '', actions: ['write'] }, { scope: 'npm', actions: ['read', 'browse'] }], global: [] })
    expect(perms).toEqual([
      { action: 'read', scope: 'npm' },
      { action: 'browse', scope: 'npm' },
    ])
  })

  it('knows implications and what anonymous access may use', () => {
    expect(impliedActions('manage')).toEqual(['browse', 'read', 'manage'])
    expect(impliedActions('read')).toEqual(['read'])
    expect(repositoryOnly([{ action: 'read', scope: '*' }])).toBe(true)
    expect(repositoryOnly([{ action: 'stats-read', scope: '*' }])).toBe(false)
  })
})

describe('sources', () => {
  it('parses local and provider sources', () => {
    expect(parseSource('local')).toEqual({ kind: 'local', provider: null })
    expect(parseSource('ldap:01ABC')).toEqual({ kind: 'ldap', provider: '01ABC' })
    expect(parseSource('oidc:x:y')).toEqual({ kind: 'oidc', provider: 'x:y' })
  })
})
