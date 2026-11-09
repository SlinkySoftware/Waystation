// SPDX-License-Identifier: GPL-3.0-or-later
// Mock webhooks (contract tag `webhooks`, T-O05): three seeded webhooks (CI, chat, an inventory that is off and
// failing), a delivery log per webhook, test events and redeliveries that are attempted a moment after they are sent
// (advanced on read, no timers): a target whose host contains "fail" or "inventory" times out, everything else
// answers 200. Secrets and header values are write-only here too.
import { http, HttpResponse } from 'msw'

import type { AuditActor, FieldError, Webhook, WebhookAttempt, WebhookDelivery, WebhookDeliveryStatus, WebhookEventName, WebhookWrite } from '@/api/types'

import { recordAudit } from './audit'
import type { MockHelpers } from './tokens'

const API = '/api/v1'
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$/
const EVENTS: WebhookEventName[] = ['content.upload', 'content.delete', 'content.update', 'repository.create', 'repository.update', 'repository.delete']
const PATTERNS: readonly string[] = ['*', 'content.*', 'repository.*', ...EVENTS]
const ATTEMPT_AFTER_MS = 1500
const MIN = 60_000
const HOUR = 60 * MIN
const ADMIN: AuditActor = { kind: 'user', id: '01JC0000000000000000000ADM', name: 'admin', auth: 'session', provider: 'local', token_id: null }
const PRIVATE_HOST = /^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|\[?::1\]?$|\[?f[cd])/i

interface StoredHook {
  hook: Omit<Webhook, 'last_delivery'>
  /** Header values are kept only to show that they exist; never returned. */
  headers: Record<string, string>
}

interface SimDelivery {
  delivery: WebhookDelivery
  /** When the next simulated attempt happens (pending deliveries). */
  dueAt: number | null
}

let seq = 0
const nextId = (prefix: string) => `${prefix}01JD${String(Date.now()).slice(-8)}${String(++seq).padStart(8, '0')}`
const iso = (ms: number) => new Date(ms).toISOString()

const hooks: StoredHook[] = []
const deliveries = new Map<string, SimDelivery[]>()

function document(event: string, eventId: string, at: number, repository: string | null, details: Record<string, unknown>): string {
  return JSON.stringify({
    id: eventId,
    type: event,
    occurred_at: iso(at),
    repository,
    actor: { kind: 'user', name: 'release-bot' },
    target: repository ? { kind: event.startsWith('repository') ? 'repository' : 'raw_file', id: null, name: repository } : null,
    details,
    source: 'https://registry.example.com',
  })
}

function headersFor(hook: StoredHook, d: Pick<WebhookDelivery, 'id' | 'event' | 'event_id'>): Record<string, string> {
  return {
    ...Object.fromEntries(Object.keys(hook.headers).map((k) => [k, '***'])),
    'Content-Type': 'application/json',
    'User-Agent': 'Waystation-Webhooks (+https://github.com/SlinkySoftware/Waystation)',
    'Waystation-Event': d.event,
    'Waystation-Event-Id': d.event_id,
    'Waystation-Delivery': d.id,
    'Idempotency-Key': d.id,
    'Waystation-Attempt': '<attempt>',
    'Waystation-Timestamp': '<unix seconds>',
    'Waystation-Signature': 't=<unix seconds>,v1=<HMAC-SHA256 hex>',
  }
}

function attempt(n: number, at: number, ok: boolean, detail: { status?: number | null; error?: string | null; response?: string | null; latency?: number } = {}): WebhookAttempt {
  return {
    n,
    at: iso(at),
    outcome: ok ? 'succeeded' : 'failed',
    status_code: detail.status ?? (ok ? 200 : null),
    latency_ms: detail.latency ?? (ok ? 84 : null),
    error: detail.error ?? null,
    response: detail.response ?? (ok ? '{"ok":true}' : null),
  }
}

function addDelivery(hook: StoredHook, init: { event: string; repository: string | null; at: number; details?: Record<string, unknown>; trigger?: WebhookDelivery['trigger']; log?: WebhookAttempt[]; status?: WebhookDeliveryStatus; eventId?: string; body?: string; redeliveryOf?: string | null; dueAt?: number | null }): SimDelivery {
  const id = nextId('wd')
  const eventId = init.eventId ?? nextId('ev')
  const log = init.log ?? []
  const last = log[log.length - 1]
  const status = init.status ?? 'pending'
  const sim: SimDelivery = {
    delivery: {
      id,
      event: init.event,
      event_id: eventId,
      trigger: init.trigger ?? 'event',
      repository: init.repository,
      status,
      attempts: log.length,
      created_at: iso(init.at),
      finished_at: status === 'pending' ? null : (last?.at ?? iso(init.at)),
      next_attempt_at: status === 'pending' ? iso(init.dueAt ?? init.at) : null,
      status_code: last?.status_code ?? null,
      latency_ms: last?.latency_ms ?? null,
      error: last?.error ?? null,
      redelivery_of: init.redeliveryOf ?? null,
      request_body: init.body ?? document(init.event, eventId, init.at, init.repository, init.details ?? {}),
      request_headers: {},
      log: [...log].reverse(),
    },
    dueAt: status === 'pending' ? (init.dueAt ?? init.at) : null,
  }
  sim.delivery.request_headers = headersFor(hook, sim.delivery)
  deliveries.set(hook.hook.id, [sim, ...(deliveries.get(hook.hook.id) ?? [])])
  return sim
}

function seedHook(fields: Partial<Webhook> & Pick<Webhook, 'name' | 'url' | 'events'>, headers: Record<string, string> = {}): StoredHook {
  const now = Date.now()
  const hook: StoredHook = {
    hook: {
      id: `wh_${String(hooks.length + 1).padStart(4, '0')}${fields.name.replace(/[^a-z0-9]/g, '').slice(0, 12)}`,
      description: '',
      repositories: [],
      enabled: true,
      secret_set: true,
      header_names: Object.keys(headers),
      allow_private: false,
      timeout_s: 10,
      max_attempts: 8,
      created_at: iso(now - 30 * 24 * HOUR),
      updated_at: iso(now - 6 * 24 * HOUR),
      ...fields,
    },
    headers,
  }
  hooks.push(hook)
  deliveries.set(hook.hook.id, [])
  return hook
}

function seed(): void {
  const now = Date.now()
  const ci = seedHook({ name: 'ci-release-builds', description: 'Starts the deployment pipeline when a release is published', url: 'https://ci.example.com/hooks/waystation', events: ['content.upload'], repositories: ['raw-*'] }, { Authorization: 'Bearer example' })
  for (let i = 8; i >= 1; i--) {
    const at = now - i * 3 * HOUR - 7 * MIN
    addDelivery(ci, { event: 'content.upload', repository: 'raw-releases', at, details: { repository: 'raw-releases', path: `tools/cli-1.${10 + (8 - i)}.0.tar.gz`, size: 18_204_160 }, status: 'succeeded', log: [attempt(1, at + 400, true, { latency: 60 + i * 9 })] })
  }
  const chat = seedHook({ name: 'chat-notifications', description: 'Posts uploads and repository changes to the platform channel', url: 'https://chat.example.com/hooks/waystation', events: ['content.*', 'repository.*'] })
  const old = now - 26 * HOUR
  addDelivery(chat, { event: 'repository.update', repository: 'dockerhub-proxy', at: old, details: { repository: 'dockerhub-proxy' }, status: 'succeeded', log: [attempt(1, old + 300, true, { status: 204, response: null, latency: 141 })] })
  const recent = now - 4 * MIN
  addDelivery(chat, {
    event: 'content.upload',
    repository: 'raw-releases',
    at: recent,
    details: { repository: 'raw-releases', path: 'tools/cli-1.18.0.tar.gz', size: 18_311_022 },
    status: 'pending',
    log: [attempt(1, recent + 200, false, { status: 503, latency: 211, response: 'upstream connect error or disconnect/reset before headers' }), attempt(2, recent + 31_000, false, { status: 503, latency: 198, response: 'upstream connect error or disconnect/reset before headers' })],
    dueAt: now + 50_000,
  })
  const inventory = seedHook({ name: 'inventory-sync', description: 'Keeps the software inventory in step (paused while the receiver is replaced)', url: 'https://inventory.example.com/api/waystation/events', events: ['*'], enabled: false, max_attempts: 3 })
  const failed = now - 3 * 24 * HOUR
  addDelivery(inventory, {
    event: 'repository.create',
    repository: 'npm',
    at: failed,
    details: { repository: 'npm' },
    status: 'dead',
    log: [attempt(1, failed + 10_000, false, { error: 'timed out after 10 s', latency: 10_003 }), attempt(2, failed + 40_000, false, { error: 'timed out after 10 s', latency: 10_001 }), attempt(3, failed + 100_000, false, { error: 'timed out after 10 s', latency: 10_002 })],
  })
}
seed()

/** Run due simulated attempts: a host with "fail" or "inventory" times out, others answer 200. */
function advance(now = Date.now()): void {
  for (const stored of hooks) {
    for (const sim of deliveries.get(stored.hook.id) ?? []) {
      if (sim.dueAt === null || sim.dueAt > now) continue
      const d = sim.delivery
      const n = d.attempts + 1
      const host = new URL(stored.hook.url).hostname
      const ok = !/fail|inventory/.test(host)
      const result = ok ? attempt(n, now, true, { latency: 70 + (n % 5) * 13 }) : attempt(n, now, false, { error: `timed out after ${stored.hook.timeout_s} s`, latency: stored.hook.timeout_s * 1000 + 2 })
      d.log = [result, ...d.log]
      d.attempts = n
      d.status_code = result.status_code
      d.latency_ms = result.latency_ms
      d.error = result.error
      const maxAttempts = d.trigger === 'test' ? 1 : stored.hook.max_attempts
      if (ok) d.status = 'succeeded'
      else if (n >= maxAttempts) d.status = 'dead'
      if (d.status === 'pending') {
        sim.dueAt = now + 30_000 * 2 ** (n - 1)
        d.next_attempt_at = iso(sim.dueAt)
      } else {
        sim.dueAt = null
        d.next_attempt_at = null
        d.finished_at = iso(now)
      }
    }
  }
}

function summary(d: WebhookDelivery) {
  const { redelivery_of: _r, request_body: _b, request_headers: _h, log: _l, ...rest } = d
  return rest
}

function view(stored: StoredHook): Webhook {
  const last = deliveries.get(stored.hook.id)?.[0]
  return { ...stored.hook, last_delivery: last ? summary(last.delivery) : null }
}

export function validateWebhook(body: WebhookWrite, existing?: StoredHook): FieldError[] {
  const errors: FieldError[] = []
  if (!NAME_RE.test(body.name ?? '')) errors.push({ field: 'name', message: 'letters, digits, dot, dash and underscore (2-63)', code: 'pattern' })
  else if (hooks.some((h) => h !== existing && h.hook.name.toLowerCase() === body.name.toLowerCase())) errors.push({ field: 'name', message: 'a webhook with this name already exists', code: 'taken' })
  let url: URL | null = null
  try {
    url = new URL(body.url)
  } catch {
    errors.push({ field: 'url', message: 'not a valid URL', code: 'invalid' })
  }
  if (url) {
    if (url.protocol !== 'http:' && url.protocol !== 'https:') errors.push({ field: 'url', message: 'must be an http:// or https:// URL', code: 'invalid' })
    else if (url.username || url.password) errors.push({ field: 'url', message: 'credentials in the URL are not allowed: use a custom header', code: 'invalid' })
    else if (!body.allow_private && (PRIVATE_HOST.test(url.hostname) || url.hostname === 'localhost')) errors.push({ field: 'url', message: 'is a private or reserved address: allow private targets to use it', code: 'private_address' })
  }
  if (!body.events?.length || body.events.some((e) => !PATTERNS.includes(e))) errors.push({ field: 'events', message: 'choose at least one known event', code: 'invalid' })
  if (!existing && !body.secret) errors.push({ field: 'secret', message: 'a signing secret is required', code: 'required' })
  if (body.secret && body.secret.length < 16) errors.push({ field: 'secret', message: 'at least 16 characters', code: 'too_short' })
  for (const name of Object.keys(body.headers ?? {})) {
    const lower = name.toLowerCase()
    if (lower.startsWith('waystation-') || ['host', 'content-type', 'content-length', 'user-agent', 'idempotency-key'].includes(lower)) errors.push({ field: `headers.${name}`, message: 'is set by Waystation', code: 'header' })
  }
  if (body.timeout_s != null && (body.timeout_s < 1 || body.timeout_s > 30)) errors.push({ field: 'timeout_s', message: '1 to 30', code: 'range' })
  if (body.max_attempts != null && (body.max_attempts < 1 || body.max_attempts > 20)) errors.push({ field: 'max_attempts', message: '1 to 20', code: 'range' })
  return errors
}

export function webhookHandlers(h: MockHelpers) {
  const find = (id: unknown) => hooks.find((s) => s.hook.id === id)

  function save(body: WebhookWrite, existing?: StoredHook): Response {
    const errors = validateWebhook(body, existing)
    if (errors.some((e) => e.code === 'taken')) return h.problem(409, 'Conflict', 'A webhook with this name already exists.', errors)
    if (errors.length) return h.problem(422, 'Unprocessable Content', 'The request has invalid fields.', errors)
    const now = iso(Date.now())
    const headers = body.headers ?? existing?.headers ?? {}
    const stored: StoredHook = {
      hook: {
        id: existing?.hook.id ?? nextId('wh'),
        name: body.name,
        description: body.description ?? '',
        url: body.url,
        events: body.events,
        repositories: [...new Set(body.repositories ?? [])],
        enabled: body.enabled ?? true,
        secret_set: true,
        header_names: Object.keys(headers).sort((a, b) => a.localeCompare(b)),
        allow_private: body.allow_private ?? false,
        timeout_s: body.timeout_s ?? 10,
        max_attempts: body.max_attempts ?? 8,
        created_at: existing?.hook.created_at ?? now,
        updated_at: now,
      },
      headers,
    }
    if (existing) hooks.splice(hooks.indexOf(existing), 1, stored)
    else {
      hooks.push(stored)
      deliveries.set(stored.hook.id, [])
    }
    recordAudit({ action: existing ? 'webhook.update' : 'webhook.create', outcome: 'success', actor: ADMIN, target: { kind: 'webhook', id: stored.hook.id, name: stored.hook.name } })
    return HttpResponse.json(view(stored), { status: existing ? 200 : 201 })
  }

  function send(stored: StoredHook, init: Parameters<typeof addDelivery>[1]): SimDelivery {
    return addDelivery(stored, { ...init, dueAt: Date.now() + ATTEMPT_AFTER_MS })
  }

  return [
    http.get(`${API}/webhooks`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      advance()
      const items = [...hooks].sort((a, b) => a.hook.name.localeCompare(b.hook.name)).map(view)
      return HttpResponse.json({ items, events: EVENTS })
    }),
    http.post(`${API}/webhooks`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      return save((await request.json()) as WebhookWrite)
    }),
    http.get(`${API}/webhooks/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      advance()
      const stored = find(params.id)
      return stored ? HttpResponse.json(view(stored)) : h.notFound()
    }),
    http.put(`${API}/webhooks/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const stored = find(params.id)
      if (!stored) return h.notFound()
      return save((await request.json()) as WebhookWrite, stored)
    }),
    http.delete(`${API}/webhooks/:id`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const stored = find(params.id)
      if (!stored) return h.notFound()
      hooks.splice(hooks.indexOf(stored), 1)
      deliveries.delete(stored.hook.id)
      recordAudit({ action: 'webhook.delete', outcome: 'success', actor: ADMIN, target: { kind: 'webhook', id: stored.hook.id, name: stored.hook.name } })
      return new HttpResponse(null, { status: 204 })
    }),
    http.post(`${API}/webhooks/:id/test`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const stored = find(params.id)
      if (!stored) return h.notFound()
      const sim = send(stored, { event: 'webhook.ping', repository: null, at: Date.now(), trigger: 'test', details: { message: 'Test event from Waystation', webhook: stored.hook.name } })
      recordAudit({ action: 'webhook.test', outcome: 'success', actor: ADMIN, target: { kind: 'webhook', id: stored.hook.id, name: stored.hook.name }, details: { delivery_id: sim.delivery.id } })
      return HttpResponse.json({ delivery_id: sim.delivery.id }, { status: 202 })
    }),
    http.get(`${API}/webhooks/:id/deliveries`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      advance()
      const stored = find(params.id)
      if (!stored) return h.notFound()
      const query = new URL(request.url).searchParams
      const status = query.get('status')
      const limit = Math.min(200, Number(query.get('limit') ?? 50) || 50)
      const offset = Number(query.get('cursor') ?? 0) || 0
      const all = (deliveries.get(stored.hook.id) ?? []).map((s) => s.delivery).filter((d) => !status || d.status === status)
      const page = all.slice(offset, offset + limit)
      return HttpResponse.json({ items: page.map(summary), next_cursor: offset + limit < all.length ? String(offset + limit) : null })
    }),
    http.get(`${API}/webhooks/:id/deliveries/:delivery`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      advance()
      const sim = deliveries.get(String(params.id))?.find((s) => s.delivery.id === params.delivery)
      return sim ? HttpResponse.json(sim.delivery) : h.notFound()
    }),
    http.post(`${API}/webhooks/:id/deliveries/:delivery/redeliver`, async ({ request, params }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const stored = find(params.id)
      const original = deliveries.get(String(params.id))?.find((s) => s.delivery.id === params.delivery)
      if (!stored || !original) return h.notFound()
      const d = original.delivery
      const sim = send(stored, { event: d.event, repository: d.repository, at: Date.now(), trigger: 'redeliver', eventId: d.event_id, body: d.request_body, redeliveryOf: d.id })
      recordAudit({ action: 'webhook.redeliver', outcome: 'success', actor: ADMIN, target: { kind: 'webhook', id: stored.hook.id, name: stored.hook.name }, details: { delivery_id: sim.delivery.id, redelivery_of: d.id, event: d.event } })
      return HttpResponse.json({ delivery_id: sim.delivery.id }, { status: 202 })
    }),
  ]
}
