// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository, RepositoryWrite } from '@/api/types'

import { isPrerelease, rubygemsGemList, rubygemsVersionsFor } from './fixtures/rubygems'
import { validatePrefetch } from './prefetch'
import { setupGuide } from './setup'

describe('mock RubyGems', () => {
  it('lists rubygems.org gems in a proxy, own gems in a hosted repository, both in a group', () => {
    const proxy = rubygemsGemList('proxy').map((g) => g.name)
    expect(proxy).toEqual(expect.arrayContaining(['nokogiri', 'rack', 'rake']))
    expect(rubygemsGemList('hosted').every((g) => g.name.startsWith('example-'))).toBe(true)
    expect(rubygemsGemList('group').length).toBe(proxy.length + rubygemsGemList('hosted').length)
    expect(rubygemsGemList('proxy', 'RA').map((g) => g.name)).toEqual(['rack', 'rails', 'rake'])
  })

  it('returns versions newest first, one row per platform build, with the latest release', () => {
    const found = rubygemsVersionsFor('rubygems-proxy', 'proxy', 'nokogiri')!
    expect(found.gem).toMatchObject({ name: 'nokogiri', latest_version: '1.18.10' })
    expect(found.items[0]).toMatchObject({ version: '1.18.10', platform: 'ruby', prerelease: false, cached: true, path: 'gems/nokogiri-1.18.10.gem' })
    expect(found.items[1]).toMatchObject({ version: '1.18.10', platform: 'x86_64-linux-gnu', path: 'gems/nokogiri-1.18.10-x86_64-linux-gnu.gem' })
    expect(found.items[0]!.sha256).toMatch(/^[0-9a-f]{64}$/)
    expect(found.items[0]!.download_url).toBe(`https://registry.example.com/repository/rubygems-proxy/gems/nokogiri-1.18.10.gem`)
    expect(found.items[0]!.dependencies.map((d) => d.name)).toEqual(['mini_portile2', 'racc'])
    expect(rubygemsVersionsFor('rubygems-proxy', 'proxy', 'Nokogiri')).toBeNull()
    expect(rubygemsVersionsFor('rubygems-proxy', 'proxy', 'example-billing')).toBeNull()
    const rails = rubygemsVersionsFor('rubygems-proxy', 'proxy', 'rails')!
    expect(rails.items.find((v) => v.version === '8.1.0.rc1')).toMatchObject({ prerelease: true })
    expect(rubygemsVersionsFor('rubygems-hosted', 'hosted', 'example-billing')!.items.every((v) => v.cached && v.uploaded_by === 'ci-builder')).toBe(true)
    expect(['1.0.0', '2.1.0.beta1', '1.0.0.pre', '8.1.0.rc1'].map(isPrerelease)).toEqual([false, true, true, true])
  })

  it('offers Bundler and the gem CLI, and gem push only on hosted repositories', () => {
    const guide = (repo: Partial<Repository>) => setupGuide({ name: 'rubygems-hosted', format: 'rubygems', online: true, ...repo } as Repository)
    const ids = (repo: Partial<Repository>) => guide(repo).clients.flatMap((c) => c.snippets.map((s) => s.id))
    const group: Partial<Repository> = { type: 'group', group: { members: ['rubygems-hosted', 'rubygems-proxy'], writable_member: null } }
    expect(guide({ type: 'proxy' }).clients.map((c) => c.id)).toEqual(['bundler', 'gem'])
    expect(ids({ type: 'hosted' })).toContain('gem-push')
    expect(ids({ type: 'proxy' })).not.toContain('gem-push')
    expect(guide(group).clients[0]!.snippets[0]!.description).toContain('One source for the members, in order: rubygems-hosted, rubygems-proxy.')
    expect(guide({ type: 'proxy' }).clients[0]!.snippets[0]!.content).toContain('source "https://registry.example.com/repository/rubygems-hosted/"')
  })

  it('accepts gem, gem:version and gem:version-platform warm items', () => {
    const errors = (warm: string[]) => validatePrefetch({ proxy: { prefetch: { warm } } } as unknown as RepositoryWrite, 'rubygems').map((e) => e.field)
    expect(errors(['rack', 'rake:13.2.1', 'nokogiri:1.18.8-x86_64-linux', 'rails:8.1.0.rc1'])).toEqual([])
    expect(errors(['rake@13.2.1', 'rack', 'a b', 'rake:'])).toEqual(['proxy.prefetch.warm.0', 'proxy.prefetch.warm.2', 'proxy.prefetch.warm.3'])
  })
})
