// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository, RepositoryWrite } from '@/api/types'

import { apkPackageList, apkVersionsFor } from './fixtures/apk'
import { validatePrefetch } from './prefetch'
import { setupGuide } from './setup'

describe('mock Alpine apk', () => {
  it('lists Alpine packages in a proxy and own packages in a hosted repository', () => {
    const proxy = apkPackageList('proxy').map((p) => p.name)
    expect(proxy).toEqual(expect.arrayContaining(['busybox', 'ca-certificates', 'curl', 'musl', 'openssl']))
    expect(apkPackageList('hosted').every((p) => p.name.startsWith('example-'))).toBe(true)
    expect(apkPackageList('proxy', 'CURL').map((p) => p.name)).toEqual(['curl', 'libcurl'])
    expect(apkPackageList('proxy').find((p) => p.name === 'libcurl')).toMatchObject({ origin: 'curl', arches: ['aarch64', 'x86_64'] })
  })

  it('returns files newest first, one per architecture, with APKINDEX checksums', () => {
    const found = apkVersionsFor('alpine-proxy', 'proxy', 'busybox')!
    expect(found.package).toMatchObject({ name: 'busybox', latest_version: '1.37.0-r19', version_count: 3 })
    expect(found.items[0]).toMatchObject({ version: '1.37.0-r19', arch: 'x86_64', branch: 'v3.22', repository: 'main', directory: 'v3.22/main/x86_64', path: 'v3.22/main/x86_64/busybox-1.37.0-r19.apk', cached: true, verified: true })
    expect(found.items[1]).toMatchObject({ version: '1.37.0-r19', arch: 'aarch64' })
    expect(found.items.every((v) => /^Q1[A-Za-z0-9+/]{27}=$/.test(v.checksum ?? ''))).toBe(true)
    expect(found.items[0]!.download_url).toBe('https://registry.example.com/repository/alpine-proxy/v3.22/main/x86_64/busybox-1.37.0-r19.apk')
    expect(found.items[1]!.depends).toContain('so:libc.musl-aarch64.so.1')
    expect(apkVersionsFor('alpine-proxy', 'proxy', 'example-agent')).toBeNull()
    expect(apkVersionsFor('apk-hosted', 'hosted', 'example-agent')!.items.every((v) => v.cached && v.verified && v.uploaded_by === 'ci-builder')).toBe(true)
  })

  it('writes /etc/apk/repositories lines for the configured branch and repositories; hosted adds the key and uploads', () => {
    const guide = (repo: Partial<Repository>) => setupGuide({ name: 'alpine-proxy', format: 'apk', online: true, ...repo } as Repository)
    const proxy = guide({ type: 'proxy' }).clients[0]!
    expect(proxy.snippets[0]!.content).toBe('https://registry.example.com/repository/alpine-proxy/latest-stable/main\nhttps://registry.example.com/repository/alpine-proxy/latest-stable/community')
    expect(guide({ type: 'proxy', format_options: { branch: 'v3.22', repositories: ['main'], architectures: ['x86_64'] } }).clients[0]!.snippets[0]!.content).toBe('https://registry.example.com/repository/alpine-proxy/v3.22/main')
    const ids = guide({ type: 'hosted', name: 'apk-hosted' }).clients[0]!.snippets.map((s) => s.id)
    expect(ids).toEqual(expect.arrayContaining(['key', 'upload']))
    expect(proxy.snippets.map((s) => s.id)).not.toContain('upload')
  })

  it('accepts name, name=version and branch/repository/arch/name=version warm items', () => {
    const errors = (warm: string[]) => validatePrefetch({ proxy: { prefetch: { warm } } } as unknown as RepositoryWrite, 'apk').map((e) => e.field)
    expect(errors(['busybox=1.37.0-r19', 'curl', 'v3.22/main/x86_64/musl=1.2.5-r10', 'gtk+3.0', 'openssl=3.5.0_rc1-r0', 'tzdata=2025b-r0'])).toEqual([])
    expect(errors(['curl@8.14.1', 'curl', 'main/x86_64/curl', '../main/x86_64/curl', 'curl=8.14.1-r', 'curl='])).toEqual(['proxy.prefetch.warm.0', 'proxy.prefetch.warm.2', 'proxy.prefetch.warm.3', 'proxy.prefetch.warm.4', 'proxy.prefetch.warm.5'])
  })
})
