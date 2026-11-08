// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { SearchHit } from '@/api/types'
import { navigation } from '@/layouts/navigation'

import { actionItems, moveActive, pageItems, type PaletteItem, rank, resultItem, score, searchItem, toSections } from './palette'

const item = (id: string, label: string, keywords?: string): PaletteItem => ({ id, group: 'repositories', label, keywords })

describe('palette ranking', () => {
  it('scores prefix over word start over substring', () => {
    expect(score('dockerhub-proxy', 'dock')).toBe(3)
    expect(score('dockerhub-proxy', 'proxy')).toBe(2)
    expect(score('@types/node', 'node')).toBe(2)
    expect(score('debian-security', 'curity')).toBe(1)
    expect(score('npm', 'yum')).toBe(0)
  })

  it('ranks matches best first, keeps order within a score, matches keywords and limits', () => {
    const items = [item('a', 'ubuntu-security'), item('b', 'debian'), item('c', 'debian-security'), item('d', 'raw', 'debian mirror')]
    expect(rank(items, 'deb', 10).map((i) => i.id)).toEqual(['b', 'c', 'd'])
    expect(rank(items, 'security', 10).map((i) => i.id)).toEqual(['a', 'c'])
    expect(rank(items, 'deb', 1).map((i) => i.id)).toEqual(['b'])
  })

  it('moves the active option with wrap-around and jumps', () => {
    const items = [item('a', 'a'), item('b', 'b'), item('c', 'c')]
    expect(moveActive(items, null, 1)).toBe('a')
    expect(moveActive(items, null, -1)).toBe('c')
    expect(moveActive(items, 'c', 1)).toBe('a')
    expect(moveActive(items, 'a', -1)).toBe('c')
    expect(moveActive(items, 'a', 5)).toBe('c')
    expect(moveActive(items, 'b', 'start')).toBe('a')
    expect(moveActive(items, 'b', 'end')).toBe('c')
    expect(moveActive([], 'a', 1)).toBeNull()
  })
})

describe('palette items', () => {
  it('offers built pages the user may open, never planned ones', () => {
    const user = pageItems(navigation, () => false).map((i) => i.id)
    expect(user).toContain('page:search')
    expect(user).toContain('page:repositories')
    expect(user).not.toContain('page:admin-auth')
    expect(user).not.toContain('page:tasks')
    expect(user).toContain('page:cleanup')
    expect(user).toContain('page:activity')
    expect(pageItems(navigation, () => true).map((i) => i.id)).toContain('page:admin-auth')
  })

  it('offers "create repository" only with the capability', () => {
    expect(actionItems(() => false).map((i) => i.id)).toEqual(['action:theme'])
    expect(actionItems(() => true).map((i) => i.id)).toEqual(['action:create', 'action:theme'])
  })

  it('turns a hit into a Browse link and remembers it', () => {
    const hit: SearchHit = {
      kind: 'image',
      format: 'docker',
      repository: 'dockerhub-proxy',
      repository_type: 'proxy',
      name: 'library/nginx',
      version: '1.27',
      qualifier: null,
      path: null,
      size: 100,
      sha256: null,
      updated_at: null,
      cached: true,
      browse: { repository: 'dockerhub-proxy', query: { image: 'library/nginx', ref: '1.27' } },
    }
    const result = resultItem(hit, 'docker')
    expect(result.label).toBe('library/nginx 1.27')
    expect(result.to).toEqual({ name: 'repository', params: { name: 'dockerhub-proxy', tab: 'browse' }, query: { image: 'library/nginx', ref: '1.27' } })
    expect(result.remember).toMatchObject({ kind: 'hit', repository: 'dockerhub-proxy' })
  })

  it('groups sections in a fixed order and drops empty ones', () => {
    const sections = toSections([item('r', 'debian'), searchItem('deb')])
    expect(sections.map((s) => s.group)).toEqual(['search', 'repositories'])
    expect(sections[0]!.items[0]!.to).toEqual({ name: 'search', query: { q: 'deb' } })
  })
})
