// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository } from '@/api/types'

import { pypiProjectList, pypiVersionsFor, pypiYank } from './fixtures/pypi'
import { setupGuide } from './setup'

describe('mock PyPI', () => {
  it('lists projects and versions newest first, with wheels and sdists', () => {
    expect(pypiProjectList().map((p) => p.name)).toContain('requests')
    const found = pypiVersionsFor('pypi-proxy', false, 'Requests')!
    expect(found.project.latest).toBe('2.32.5')
    expect(found.versions[0]!.version).toBe('2.32.5')
    expect(found.versions[0]!.files.map((f) => f.kind).sort()).toEqual(['sdist', 'wheel'])
    expect(pypiVersionsFor('pypi-proxy', false, 'no-such-project')).toBeNull()
  })

  it('remembers yanks per repository', () => {
    expect(pypiYank('pypi-internal', 'six', '1.17.0', 'broken', true)).toBe(true)
    expect(pypiVersionsFor('pypi-internal', true, 'six')!.versions[0]).toMatchObject({ version: '1.17.0', yanked: true })
    expect(pypiVersionsFor('pypi-proxy', false, 'six')!.versions[0]!.yanked).toBe(false)
    expect(pypiYank('pypi-internal', 'six', '9.9.9', null, true)).toBe(false)
  })

  it('offers upload snippets only where uploads are accepted', () => {
    const ids = (repo: Partial<Repository>) => setupGuide({ name: 'p', format: 'pypi', online: true, ...repo } as Repository).clients.map((c) => c.id)
    expect(ids({ type: 'proxy' })).toEqual(['pip', 'uv', 'poetry', 'pdm'])
    expect(ids({ type: 'hosted' })).toContain('twine')
    expect(ids({ type: 'group', group: { members: ['a'], writable_member: null } })).not.toContain('twine')
  })
})
