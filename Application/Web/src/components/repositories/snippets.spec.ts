// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { SetupSnippet } from '@/api/types'

import { detectOs, fillSnippet, hasOsVariants, needsToken, snippetsFor, TOKEN_PLACEHOLDER } from './snippets'

const s = (id: string, os: SetupSnippet['os'] = null, content = id): SetupSnippet => ({ id, title: id, language: 'shell', content, os })

describe('Set me up placeholders', () => {
  it('fills the username and keeps a visible token placeholder', () => {
    const out = fillSnippet("echo '{{TOKEN}}' | docker login registry.example.com --username {{USERNAME}}", 'alice')
    expect(out).toBe(`echo '${TOKEN_PLACEHOLDER}' | docker login registry.example.com --username alice`)
  })

  it('falls back to a username placeholder', () => {
    expect(fillSnippet('login {{USERNAME}}', null)).toBe('login <username>')
  })

  it('picks the OS variant of each snippet, else the base one, in order', () => {
    const list = [s('a'), s('mirror', null, 'linux'), s('mirror', 'windows', 'win'), s('mirror', 'macos', 'mac'), s('b'), s('win-only', 'windows')]
    expect(hasOsVariants(list)).toBe(true)
    expect(hasOsVariants([s('a')])).toBe(false)
    expect(snippetsFor(list, 'linux').map((x) => x.content)).toEqual(['a', 'linux', 'b'])
    expect(snippetsFor(list, 'windows').map((x) => x.content)).toEqual(['a', 'win', 'b', 'win-only'])
    expect(snippetsFor(list, 'macos').map((x) => x.content)).toEqual(['a', 'mac', 'b'])
  })

  it('guesses the OS from the user agent', () => {
    expect(detectOs('Mozilla/5.0 (Windows NT 10.0; Win64; x64)')).toBe('windows')
    expect(detectOs('Mozilla/5.0 (Macintosh; Intel Mac OS X 15_1)')).toBe('macos')
    expect(detectOs('Mozilla/5.0 (X11; Linux x86_64)')).toBe('linux')
    expect(detectOs('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)')).toBe('linux')
  })

  it('detects snippets that need a token', () => {
    expect(needsToken('a {{TOKEN}}')).toBe(true)
    expect(needsToken('plain')).toBe(false)
  })
})
