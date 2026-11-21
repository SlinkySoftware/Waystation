// SPDX-License-Identifier: GPL-3.0-or-later
// Fill "Set me up" placeholders. The token is never known to the UI (shown once at creation, T-S04), so it stays a
// visible placeholder the user replaces; the username can be filled in.
import type { SetupSnippet } from '@/api/types'

export const TOKEN_PLACEHOLDER = '<your-api-token>'

export type SetupOs = 'linux' | 'macos' | 'windows'
export const SETUP_OS: readonly SetupOs[] = ['linux', 'macos', 'windows']

/** The visitor's OS from the user agent (the switch's default); Linux for anything else. */
export function detectOs(userAgent: string): SetupOs {
  if (/Windows/i.test(userAgent)) return 'windows'
  if (/Macintosh|Mac OS X/i.test(userAgent) && !/iPhone|iPad/i.test(userAgent)) return 'macos'
  return 'linux'
}

/** Whether any snippet differs by OS (then the panel shows the OS switch). */
export function hasOsVariants(snippets: readonly SetupSnippet[]): boolean {
  return snippets.some((s) => s.os)
}

/** The snippets as they read on `os`: a snippet with `os` replaces the one of the same id without; order kept. */
export function snippetsFor(snippets: readonly SetupSnippet[], os: SetupOs): SetupSnippet[] {
  const out: SetupSnippet[] = []
  const seen = new Set<string>()
  for (const s of snippets) {
    if (seen.has(s.id)) continue
    const pick = snippets.find((v) => v.id === s.id && v.os === os) ?? snippets.find((v) => v.id === s.id && !v.os)
    if (!pick) continue
    seen.add(s.id)
    out.push(pick)
  }
  return out
}

export function fillSnippet(content: string, username: string | null): string {
  return content.replaceAll('{{TOKEN}}', TOKEN_PLACEHOLDER).replaceAll('{{USERNAME}}', username || '<username>')
}

export function needsToken(content: string): boolean {
  return content.includes('{{TOKEN}}')
}
