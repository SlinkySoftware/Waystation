// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository, RepositoryWrite } from '@/api/types'

import { composerPackageList, composerVersionsFor, stabilityOf } from './fixtures/composer'
import { validatePrefetch } from './prefetch'
import { setupGuide } from './setup'

describe('mock Composer', () => {
  it('lists Packagist packages in a proxy, own packages in a hosted repository, both in a group', () => {
    const proxy = composerPackageList('proxy').map((p) => p.name)
    expect(proxy).toEqual(expect.arrayContaining(['monolog/monolog', 'psr/container', 'psr/log']))
    expect(composerPackageList('hosted').every((p) => p.name.startsWith('acme/'))).toBe(true)
    expect(composerPackageList('group').length).toBe(proxy.length + composerPackageList('hosted').length)
    expect(composerPackageList('proxy', 'PSR/').map((p) => p.name)).toEqual(['psr/container', 'psr/log'])
  })

  it('returns versions newest first with the latest stable version and stabilities', () => {
    const found = composerVersionsFor('composer-proxy', 'proxy', 'Monolog/Monolog')!
    expect(found.package).toMatchObject({ name: 'monolog/monolog', latest_version: '3.9.0', type: 'library' })
    expect(found.items.at(-1)).toMatchObject({ version: 'dev-main', stability: 'dev' })
    expect(found.items[0]).toMatchObject({ version: '3.9.0', stability: 'stable', cached: true })
    expect(found.items[0]!.version_normalized).toBe(['3', '9', '0', '0'].join('.'))
    expect(found.items[0]!.reference).toMatch(/^[0-9a-f]{40}$/)
    expect(found.items[0]!.download_url).toBe(`https://registry.example.com/repository/composer-proxy/dists/monolog/monolog/${found.items[0]!.version_normalized}/${found.items[0]!.reference}.zip`)
    expect(composerVersionsFor('composer-proxy', 'proxy', 'acme/billing')).toBeNull()
    expect(composerVersionsFor('composer-hosted', 'hosted', 'acme/billing')!.items.every((v) => v.cached && v.uploaded_by === 'ci-builder')).toBe(true)
    expect(['v7.3.0-RC1', '2.1.0-beta.1', '1.0.0-alpha.2', 'dev-main', '1.2.3'].map(stabilityOf)).toEqual(['RC', 'beta', 'alpha', 'dev', 'stable'])
  })

  it('offers upload and delete only on hosted repositories', () => {
    const guide = (repo: Partial<Repository>) => setupGuide({ name: 'composer-hosted', format: 'composer', online: true, ...repo } as Repository)
    const ids = (repo: Partial<Repository>) => guide(repo).clients.flatMap((c) => c.snippets.map((s) => s.id))
    const group: Partial<Repository> = { type: 'group', group: { members: ['composer-hosted', 'composer-proxy'], writable_member: null } }
    expect(guide({ type: 'proxy' }).clients.map((c) => c.id)).toEqual(['composer'])
    expect(ids({ type: 'proxy' })).toEqual(['composer-repo', 'composer-auth', 'composer-json', 'composer-require'])
    expect(ids({ type: 'hosted' })).toEqual(expect.arrayContaining(['composer-upload', 'composer-delete']))
    expect(ids(group)).not.toContain('composer-upload')
    expect(guide(group).clients[0]!.snippets[0]!.description).toContain('One repository for the members, in order: composer-hosted, composer-proxy.')
    const snippets = guide({ type: 'proxy' }).clients[0]!.snippets
    expect(snippets[0]!.content).toBe('composer config repositories.waystation composer https://registry.example.com/repository/composer-hosted/\ncomposer config repositories.packagist.org false')
    expect(snippets.find((s) => s.id === 'composer-auth')!.content).toBe('composer config http-basic.registry.example.com {{USERNAME}} {{TOKEN}}')
  })

  it('accepts vendor/name and vendor/name:version warm items', () => {
    const errors = (warm: string[]) => validatePrefetch({ proxy: { prefetch: { warm } } } as unknown as RepositoryWrite, 'composer').map((e) => e.field)
    expect(errors(['psr/log', 'monolog/monolog:3.9.0'])).toEqual([])
    expect(errors(['monolog', 'psr/log', 'a b/c'])).toEqual(['proxy.prefetch.warm.0', 'proxy.prefetch.warm.2'])
  })
})
