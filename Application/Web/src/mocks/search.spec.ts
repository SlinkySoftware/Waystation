// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { seedRepositories } from './fixtures/repositories'
import { mockSearch, type SearchParams } from './search'

const repos = seedRepositories()
const params = (p: Partial<SearchParams>): SearchParams => ({ q: '', formats: [], repositories: [], kinds: [], types: [], cached: false, ...p })

describe('mock search', () => {
  it('matches the start of a name or of its last segment, case-insensitively', () => {
    const page = mockSearch(repos, params({ q: 'NGINX' }), 0, 100, true)
    expect(page.mode).toBe('name')
    expect(page.items.length).toBeGreaterThan(0)
    for (const hit of page.items) {
      const lc = hit.name.toLowerCase()
      expect(lc.startsWith('nginx') || lc.slice(lc.lastIndexOf('/') + 1).startsWith('nginx')).toBe(true)
    }
    expect(page.items.some((h) => h.name === 'library/nginx')).toBe(true)
  })

  it('pages with a cursor and only returns facets on the first page', () => {
    const first = mockSearch(repos, params({ q: 'lib' }), 0, 5, true)
    expect(first.items).toHaveLength(5)
    expect(first.next_cursor).not.toBeNull()
    expect(first.facets?.total).toBeGreaterThan(5)
    const second = mockSearch(repos, params({ q: 'lib' }), 5, 5, true)
    expect(second.facets).toBeNull()
    expect(second.items[0]).not.toEqual(first.items[0])
  })

  it('computes each facet with the other filters applied', () => {
    const page = mockSearch(repos, params({ q: 'nginx', formats: ['apt'] }), 0, 100, true)
    expect(new Set(page.items.map((h) => h.format))).toEqual(new Set(['apt']))
    const formats = Object.fromEntries(page.facets!.format.map((f) => [f.value, f.count]))
    expect(formats.docker).toBeGreaterThan(0)
    expect(page.facets!.total).toBe(page.items.length)
  })

  it('leaves out apt/yum listings that are not cached with cached-only, and finds digests', () => {
    const cached = mockSearch(repos, params({ q: 'nginx', cached: true }), 0, 500, false)
    expect(cached.items.every((h) => h.cached !== false)).toBe(true)
    const image = mockSearch(repos, params({ q: 'nginx' }), 0, 100, false).items.find((h) => h.kind === 'image')!
    const digest = mockSearch(repos, params({ q: image.sha256!.slice(0, 20) }), 0, 100, true)
    expect(digest.mode).toBe('digest')
    expect(digest.items.some((h) => h.name === image.name && h.version === image.version)).toBe(true)
  })
})
