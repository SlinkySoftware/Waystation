// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { browseLocation, hasFilters, highlight, isActive, isDigest, stateFromQuery, stateToQuery, toggle } from './search'

describe('search URL state', () => {
  it('round-trips query, repeated facets and cached-only, dropping junk', () => {
    const state = stateFromQuery({ q: 'nginx 1.27', format: ['apt', 'docker', 'apt'], repository: 'debian', kind: ['package', 'folder'], type: 'hosted', cached: '1' })
    expect(state).toEqual({ q: 'nginx 1.27', format: ['apt', 'docker'], repository: ['debian'], kind: ['package'], type: ['hosted'], cached: true })
    expect(stateToQuery(state)).toEqual({ q: 'nginx 1.27', format: ['apt', 'docker'], repository: ['debian'], kind: ['package'], type: ['hosted'], cached: '1' })
    expect(stateFromQuery(stateToQuery(state) as never)).toEqual(state)
  })

  it('knows when there is something to search for', () => {
    const empty = stateFromQuery({})
    expect(isActive(empty)).toBe(false)
    expect(hasFilters(empty)).toBe(false)
    expect(stateToQuery(empty)).toEqual({})
    expect(isActive({ ...empty, q: '  ' })).toBe(false)
    expect(isActive({ ...empty, format: ['npm'] })).toBe(true)
    expect(isActive({ ...empty, q: 'react' })).toBe(true)
  })

  it('toggles a facet value', () => {
    expect(toggle(['a'], 'b')).toEqual(['a', 'b'])
    expect(toggle(['a', 'b'], 'a')).toEqual(['b'])
  })
})

describe('search helpers', () => {
  it('links a hit to its repository Browse view', () => {
    expect(browseLocation({ browse: { repository: 'dockerhub-proxy', query: { image: 'library/nginx', ref: '1.27' } } })).toEqual({
      name: 'repository',
      params: { name: 'dockerhub-proxy', tab: 'browse' },
      query: { image: 'library/nginx', ref: '1.27' },
    })
  })

  it('recognises digests', () => {
    expect(isDigest('sha256:0123abcd')).toBe(true)
    expect(isDigest('A'.repeat(64))).toBe(true)
    expect(isDigest('sha256:abc')).toBe(false)
    expect(isDigest('deadbeef')).toBe(false)
  })

  it('highlights the matched prefix of the name or of its last segment', () => {
    expect(highlight('library/nginx', 'NGI')).toEqual([
      { text: 'library/', match: false },
      { text: 'ngi', match: true },
      { text: 'nx', match: false },
    ])
    expect(highlight('nginx-conf', 'nginx 1.2')).toEqual([
      { text: 'nginx', match: true },
      { text: '-conf', match: false },
    ])
    expect(highlight('react', 'vue')).toEqual([{ text: 'react', match: false }])
    expect(highlight('react', '')).toEqual([{ text: 'react', match: false }])
  })
})
