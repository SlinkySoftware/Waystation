// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { fillSnippet, needsToken, TOKEN_PLACEHOLDER } from './snippets'

describe('Set me up placeholders', () => {
  it('fills the username and keeps a visible token placeholder', () => {
    const out = fillSnippet("echo '{{TOKEN}}' | docker login registry.example.com --username {{USERNAME}}", 'alice')
    expect(out).toBe(`echo '${TOKEN_PLACEHOLDER}' | docker login registry.example.com --username alice`)
  })

  it('falls back to a username placeholder', () => {
    expect(fillSnippet('login {{USERNAME}}', null)).toBe('login <username>')
  })

  it('detects snippets that need a token', () => {
    expect(needsToken('a {{TOKEN}}')).toBe(true)
    expect(needsToken('plain')).toBe(false)
  })
})
