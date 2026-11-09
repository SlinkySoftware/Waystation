// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository } from '@/api/types'

import { goModuleList, goVersionsFor } from './fixtures/go'
import { setupGuide } from './setup'

describe('mock Go modules', () => {
  it('lists modules by path with their cached versions', () => {
    const modules = goModuleList().map((m) => m.module)
    expect(modules).toContain('github.com/Masterminds/semver/v3')
    expect([...modules].sort((a, b) => a.localeCompare(b))).toEqual(modules)
    expect(goModuleList().every((m) => m.cached_versions >= 1)).toBe(true)
  })

  it('shows tagged, pseudo and +incompatible versions', () => {
    const text = goVersionsFor('go-proxy', 'golang.org/x/text')!
    expect(text.module.latest).toBe('v0.30.0')
    const pseudo = text.versions.find((v) => v.pseudo)!
    expect(pseudo).toMatchObject({ listed: false, cached: true, prerelease: true })
    expect(pseudo.files.map((f) => f.kind)).toEqual(['info', 'mod', 'zip'])
    expect(goVersionsFor('go-proxy', 'github.com/docker/docker')!.versions[0]!.incompatible).toBe(true)
    expect(goVersionsFor('go-proxy', 'example.com/nothing')).toBeNull()
  })

  it('sets up GOPROXY and .netrc', () => {
    const guide = setupGuide({ name: 'go-proxy', format: 'go', type: 'proxy', online: true } as Repository)
    expect(guide.clients.map((c) => c.id)).toEqual(['go', 'go-auth'])
    expect(guide.clients[0]!.snippets[0]!.content).toContain('GOPROXY=')
  })
})
