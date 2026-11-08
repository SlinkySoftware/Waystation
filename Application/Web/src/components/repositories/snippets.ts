// SPDX-License-Identifier: GPL-3.0-or-later
// Fill "Set me up" placeholders. The token is never known to the UI (shown once at creation, T-S04), so it stays a
// visible placeholder the user replaces; the username can be filled in.
export const TOKEN_PLACEHOLDER = '<your-api-token>'

export function fillSnippet(content: string, username: string | null): string {
  return content.replaceAll('{{TOKEN}}', TOKEN_PLACEHOLDER).replaceAll('{{USERNAME}}', username || '<username>')
}

export function needsToken(content: string): boolean {
  return content.includes('{{TOKEN}}')
}
