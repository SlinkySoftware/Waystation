// SPDX-License-Identifier: GPL-3.0-or-later
// Webhooks (T-O05, SPEC §4.12): event vocabulary, editor form state <-> WebhookWrite, delivery status labels.
import type { Webhook, WebhookDeliveryStatus, WebhookDeliverySummary, WebhookEventName, WebhookWrite } from '@/api/types'
import { type MessageKey, t } from '@/i18n'
import { iconBlocked, iconOk, iconPending } from '@/utils/icons'

export const EVENT_GROUPS: { category: 'content' | 'repository'; events: WebhookEventName[] }[] = [
  { category: 'content', events: ['content.upload', 'content.delete', 'content.update'] },
  { category: 'repository', events: ['repository.create', 'repository.update', 'repository.delete'] },
]
export const ALL_EVENTS: WebhookEventName[] = EVENT_GROUPS.flatMap((g) => g.events)
export const SECRET_MIN = 16
const RESERVED = new Set(['host', 'content-length', 'content-type', 'content-encoding', 'transfer-encoding', 'connection', 'keep-alive', 'upgrade', 'te', 'trailer', 'expect', 'proxy-authorization', 'user-agent', 'idempotency-key'])
const TOKEN = /^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$/

export function eventLabel(event: string): string {
  const key = `webhooks.event.${event.replace('.', '_')}` as MessageKey
  const text = t(key)
  return text === key ? event : text
}

/** Expand the stored patterns (`*`, `content.*`, names) to the concrete events they select. */
export function expandEvents(patterns: readonly string[]): Set<WebhookEventName> {
  const out = new Set<WebhookEventName>()
  for (const p of patterns) {
    if (p === '*') ALL_EVENTS.forEach((e) => out.add(e))
    else if (p.endsWith('.*')) ALL_EVENTS.filter((e) => e.startsWith(p.slice(0, -1))).forEach((e) => out.add(e))
    else if ((ALL_EVENTS as string[]).includes(p)) out.add(p as WebhookEventName)
  }
  return out
}

/** The shortest pattern list for a selection: `*`, whole categories as `<category>.*`, else names. */
export function compactEvents(selected: Iterable<WebhookEventName>): WebhookEventName[] {
  const set = new Set(selected)
  if (ALL_EVENTS.every((e) => set.has(e))) return ['*']
  const out: WebhookEventName[] = []
  for (const group of EVENT_GROUPS) {
    if (group.events.every((e) => set.has(e))) out.push(`${group.category}.*` as WebhookEventName)
    else out.push(...group.events.filter((e) => set.has(e)))
  }
  return out
}

/** One line for the table: "All events", "Content: upload, delete" ... */
export function describeEvents(patterns: readonly string[]): string {
  const set = expandEvents(patterns)
  if (set.size === ALL_EVENTS.length) return t('webhooks.allEvents')
  return EVENT_GROUPS.filter((g) => g.events.some((e) => set.has(e)))
    .map((g) => {
      const chosen = g.events.filter((e) => set.has(e))
      const label = t(`webhooks.category.${g.category}` as MessageKey)
      return chosen.length === g.events.length ? t('webhooks.allOf', { category: label }) : `${label}: ${chosen.map((e) => t(`webhooks.verb.${e.split('.')[1]}` as MessageKey)).join(', ')}`
    })
    .join(' · ')
}

export function describeScope(globs: readonly string[]): string {
  return globs.length ? globs.join(', ') : t('webhooks.allRepositories')
}

export interface HeaderRow {
  name: string
  value: string
}

/** Why a custom header cannot be sent (null = fine); mirrors the server's check. */
export function headerProblem(name: string, value: string): string | null {
  const trimmed = name.trim()
  if (!TOKEN.test(trimmed)) return t('webhooks.editor.headerName')
  const lower = trimmed.toLowerCase()
  if (RESERVED.has(lower) || lower.startsWith('waystation-')) return t('webhooks.editor.headerReserved')
  if (value.length > 1024) return t('webhooks.editor.headerLong')
  if (/[\x00-\x08\x0a-\x1f\x7f]/.test(value)) return t('webhooks.editor.headerControl')
  return null
}

export function rowsToHeaders(rows: readonly HeaderRow[]): Record<string, string> {
  return Object.fromEntries(rows.filter((r) => r.name.trim()).map((r) => [r.name.trim(), r.value]))
}

/** A random signing secret (32 bytes, hex) made in the browser: the server never shows one back. */
export function generateSecret(bytes = 32): string {
  const buf = new Uint8Array(bytes)
  crypto.getRandomValues(buf)
  return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** Is a URL acceptable to send events to? (The server checks it again, with the SSRF rules.) */
export function urlProblem(url: string): string | null {
  let parsed: URL
  try {
    parsed = new URL(url.trim())
  } catch {
    return t('webhooks.editor.urlInvalid')
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return t('webhooks.editor.urlScheme')
  if (parsed.username || parsed.password) return t('webhooks.editor.urlCredentials')
  return null
}

/** PUT body for a stored webhook with some fields changed (secret and headers kept). */
export function toWrite(hook: Webhook, patch: Partial<WebhookWrite> = {}): WebhookWrite {
  return {
    name: hook.name,
    description: hook.description,
    url: hook.url,
    events: hook.events,
    repositories: hook.repositories,
    enabled: hook.enabled,
    secret: null,
    headers: null,
    allow_private: hook.allow_private,
    timeout_s: hook.timeout_s,
    max_attempts: hook.max_attempts,
    ...patch,
  }
}

interface Meta {
  label: string
  icon: string
  tone: string
  description: string
}

export function deliveryMeta(status: WebhookDeliveryStatus, attempts = 0): Meta {
  switch (status) {
    case 'succeeded':
      return { label: t('webhooks.status.succeeded'), icon: iconOk, tone: 'tone-ok', description: t('webhooks.status.succeededHelp') }
    case 'dead':
      return { label: t('webhooks.status.dead'), icon: iconBlocked, tone: 'tone-blocked', description: t('webhooks.status.deadHelp') }
    case 'pending':
    default:
      return attempts
        ? { label: t('webhooks.status.retrying'), icon: iconPending, tone: 'tone-degraded', description: t('webhooks.status.retryingHelp') }
        : { label: t('webhooks.status.pending'), icon: iconPending, tone: 'tone-hit', description: t('webhooks.status.pendingHelp') }
  }
}

/** "HTTP 200 · 84 ms", "Timed out after 10 s", "Waiting" ... for a delivery row. */
export function deliveryResult(d: Pick<WebhookDeliverySummary, 'status_code' | 'latency_ms' | 'error' | 'attempts'>): string {
  if (!d.attempts) return t('webhooks.notAttempted')
  const parts: string[] = []
  if (d.status_code != null) parts.push(`HTTP ${d.status_code}`)
  if (d.error) parts.push(d.error)
  if (d.latency_ms != null && d.status_code != null) parts.push(`${Math.round(d.latency_ms)} ms`)
  return parts.join(' · ')
}

/** Pretty-print a JSON request body for the log (falls back to the raw text). */
export function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}
