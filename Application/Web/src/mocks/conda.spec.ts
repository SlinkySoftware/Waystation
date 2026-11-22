// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository } from '@/api/types'

import { condaFilesFor, condaPackageList } from './fixtures/conda'
import { validatePrefetch } from './prefetch'
import { setupGuide } from './setup'

describe('mock conda channels', () => {
  it('lists conda-forge packages on proxies and an internal package on hosted channels, by name prefix', () => {
    const names = condaPackageList(false).map((p) => p.name)
    expect(names).toEqual(expect.arrayContaining(['numpy', 'python', 'tzdata', 'openssl']))
    expect([...names].sort((a, b) => a.localeCompare(b))).toEqual(names)
    expect(condaPackageList(false, 'tz').map((p) => p.name)).toEqual(['tzdata'])
    expect(condaPackageList(true).map((p) => p.name)).toEqual(['example-tools'])
  })

  it('shows files newest version first, per subdir, with an unverified fetch on the proxy', () => {
    const numpy = condaFilesFor('conda-forge', false, 'numpy')!
    expect(numpy.package).toMatchObject({ name: 'numpy', latest: '2.3.4', subdirs: ['linux-64', 'osx-arm64'] })
    expect(numpy.files[0]!.version).toBe('2.3.4')
    expect(condaFilesFor('conda-forge', false, 'numpy', 'osx-arm64')!.files.every((f) => f.subdir === 'osx-arm64')).toBe(true)
    expect(condaFilesFor('conda-forge', false, 'tzdata')!.files.some((f) => !f.verified)).toBe(true)
    const hosted = condaFilesFor('conda-hosted', true, 'example-tools')!
    expect(hosted.files.every((f) => f.uploaded_by === 'ci-builder' && f.cached)).toBe(true)
    expect(hosted.files.find((f) => f.version === '1.3.0rc1')!.prerelease).toBe(true)
    expect(condaFilesFor('conda-forge', false, 'example-tools')).toBeNull()
  })

  it('checks warm items and offers conda, micromamba and pixi snippets; uploads on hosted channels', () => {
    const repo = (type: 'proxy' | 'hosted') => ({ name: 'conda-forge', format: 'conda', type, online: true }) as Repository
    const body = { proxy: { prefetch: { refresh_popular: false, warm: ['noarch/tzdata', 'linux-64/numpy==2.1.3=py312_0', 'tzdata'], schedule: null } } } as never
    expect(validatePrefetch(body, 'conda').map((e) => e.field)).toEqual(['proxy.prefetch.warm.2'])
    expect(setupGuide(repo('proxy')).clients.map((c) => c.id)).toEqual(['conda', 'micromamba', 'pixi'])
    expect(setupGuide(repo('hosted')).clients.map((c) => c.id)).toContain('conda-upload')
  })
})
