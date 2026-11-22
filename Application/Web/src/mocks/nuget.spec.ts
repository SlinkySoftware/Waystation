// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository } from '@/api/types'

import { nugetPackageList, nugetSetListing, nugetVersionsFor } from './fixtures/nuget'
import { setupGuide } from './setup'

describe('mock NuGet', () => {
  it('lists nuget.org packages in a proxy, own packages in a hosted repository, both in a group', () => {
    const proxy = nugetPackageList('proxy').map((p) => p.id)
    expect(proxy).toEqual(expect.arrayContaining(['Newtonsoft.Json', 'Serilog', 'Humanizer.Core', 'Polly']))
    expect(nugetPackageList('hosted').every((p) => p.id.startsWith('Example.'))).toBe(true)
    expect(nugetPackageList('group').length).toBe(proxy.length + nugetPackageList('hosted').length)
    expect(nugetPackageList('proxy', 'SERILOG').map((p) => p.id)).toEqual(['Serilog', 'Serilog.Sinks.Console'])
  })

  it('returns versions newest first with the latest stable version and pre-release flags', () => {
    const found = nugetVersionsFor('nuget.org-proxy', 'proxy', 'newtonsoft.json')!
    expect(found.package).toMatchObject({ id: 'Newtonsoft.Json', name: 'newtonsoft.json', latest_version: '13.0.4' })
    expect(found.items[0]).toMatchObject({ version: '13.0.4', listed: true, cached: true, prerelease: false })
    expect(found.items[0]!.download_url).toBe('https://registry.example.com/repository/nuget.org-proxy/v3-flatcontainer/newtonsoft.json/13.0.4/newtonsoft.json.13.0.4.nupkg')
    const serilog = nugetVersionsFor('nuget.org-proxy', 'proxy', 'Serilog')!
    expect(serilog.package.latest_version).toBe('4.3.0')
    expect(serilog.items[0]!.prerelease).toBe(true)
    expect(nugetVersionsFor('nuget.org-proxy', 'proxy', 'Example.Common')).toBeNull()
  })

  it('remembers unlists per hosted repository', () => {
    expect(nugetSetListing('nuget-hosted', 'example.common', '2.3.1', false)).toBe(true)
    const version = (repo: string) => nugetVersionsFor(repo, 'hosted', 'Example.Common')!.items.find((v) => v.version === '2.3.1')!
    expect(version('nuget-hosted').listed).toBe(false)
    expect(version('other-hosted').listed).toBe(true)
    expect(nugetSetListing('nuget-hosted', 'Example.Common', '2.3.1', true)).toBe(true)
    expect(version('nuget-hosted').listed).toBe(true)
    expect(nugetSetListing('nuget-hosted', 'Example.Common', '9.9.9', false)).toBe(false)
    expect(nugetSetListing('nuget-hosted', 'Newtonsoft.Json', '13.0.4', false)).toBe(false)
  })

  it('offers push and delete only where pushes are accepted', () => {
    const guide = (repo: Partial<Repository>) => setupGuide({ name: 'nuget-hosted', format: 'nuget', online: true, ...repo } as Repository)
    const ids = (repo: Partial<Repository>) => guide(repo).clients.flatMap((c) => c.snippets.map((s) => s.id))
    expect(guide({ type: 'proxy' }).clients.map((c) => c.id)).toEqual(['dotnet', 'nuget-exe'])
    expect(ids({ type: 'proxy' })).not.toContain('dotnet-push')
    expect(ids({ type: 'hosted' })).toEqual(expect.arrayContaining(['dotnet-push', 'dotnet-delete', 'nuget-exe-push', 'nuget-config']))
    expect(ids({ type: 'group', group: { members: ['a'], writable_member: null } })).not.toContain('dotnet-push')
    expect(ids({ type: 'group', group: { members: ['a'], writable_member: 'a' } })).toContain('dotnet-push')
    const source = guide({ type: 'hosted' }).clients[0]!.snippets.find((s) => s.id === 'dotnet-source')!
    expect(source.content).toBe('dotnet nuget add source https://registry.example.com/repository/nuget-hosted/index.json --name waystation')
  })
})
