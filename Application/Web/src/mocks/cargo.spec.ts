// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository } from '@/api/types'

import { cargoCrateList, cargoVersionsFor } from './fixtures/cargo'
import { validatePrefetch } from './prefetch'
import { setupGuide } from './setup'

describe('mock Cargo crates', () => {
  it('lists public crates on proxies and internal crates on hosted repositories, by name', () => {
    const names = cargoCrateList(false).map((c) => c.name)
    expect(names).toEqual(expect.arrayContaining(['serde', 'itoa', 'ryu', 'libc', 'cfg-if']))
    expect([...names].sort((a, b) => a.localeCompare(b))).toEqual(names)
    expect(cargoCrateList(true).map((c) => c.name)).toEqual(['example-internal'])
  })

  it('shows versions newest first, yanked and pre-release, the latest non-yanked release', () => {
    const serde = cargoVersionsFor('cargo-proxy', false, 'SERDE')!
    expect(serde.crate).toMatchObject({ name: 'serde', latest: '1.0.228', cached_versions: 3 })
    expect(serde.versions[0]).toMatchObject({ version: '1.0.228', cached: true, prerelease: false, yanked: false })
    expect(serde.versions.at(-1)).toMatchObject({ cached: false, size: null })
    expect(serde.versions[0]!.cksum).toMatch(/^[0-9a-f]{64}$/)
    const internal = cargoVersionsFor('cargo-hosted', true, 'example-internal')!
    expect(internal.crate.latest).toBe('1.3.0')
    expect(internal.versions[0]).toMatchObject({ version: '2.0.0-rc.1', prerelease: true, published_by: 'ci-builder' })
    expect(internal.versions.find((v) => v.version === '1.2.0')!.yanked).toBe(true)
    expect(cargoVersionsFor('cargo-proxy', false, 'example-internal')).toBeNull()
  })

  it('checks warm items: crate@version or crate', () => {
    const body = { proxy: { prefetch: { refresh_popular: false, warm: ['serde@1.0.228', 'itoa', 'serde@^1', 'bad name'], schedule: null } } } as never
    expect(validatePrefetch(body, 'cargo').map((e) => e.field)).toEqual(['proxy.prefetch.warm.2', 'proxy.prefetch.warm.3'])
  })

  it('sets up the sparse index, credentials and, where writable, cargo publish', () => {
    const proxy = setupGuide({ name: 'cargo-proxy', format: 'cargo', type: 'proxy', online: true } as Repository)
    expect(proxy.clients.map((c) => c.id)).toEqual(['cargo'])
    expect(proxy.clients[0]!.snippets.map((s) => s.id)).toEqual(['cargo-replace', 'cargo-registry', 'cargo-credentials', 'cargo-login'])
    expect(proxy.clients[0]!.snippets[0]!.content).toContain('index = "sparse+')
    expect(proxy.clients[0]!.snippets[0]!.content).toContain('replace-with = "waystation"')
    const hosted = setupGuide({ name: 'cargo-hosted', format: 'cargo', type: 'hosted', online: true } as Repository)
    expect(hosted.clients.map((c) => c.id)).toEqual(['cargo', 'cargo-publish'])
    const group = setupGuide({ name: 'cargo-group', format: 'cargo', type: 'group', online: true, group: { members: ['cargo-hosted'], writable_member: null } } as unknown as Repository)
    expect(group.clients.map((c) => c.id)).toEqual(['cargo'])
  })
})
