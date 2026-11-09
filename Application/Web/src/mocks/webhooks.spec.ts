// SPDX-License-Identifier: GPL-3.0-or-later
import { getResponse } from 'msw'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import type { Problem, Webhook, WebhookDelivery, WebhookDeliveryAccepted, WebhookDeliveryList, WebhookList } from '@/api/types'

import { handlers } from './handlers'

const CSRF = 'mock-csrf-7f3a9c'
async function api<T>(method: string, path: string, body?: unknown): Promise<{ status: number; json: T }> {
  const request = new Request(new URL(`/api/v1${path}`, location.origin), {
    method,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const response = (await getResponse(handlers, request))!
  return { status: response.status, json: response.status === 204 ? (null as T) : ((await response.json()) as T) }
}

const write = (fields: Record<string, unknown> = {}) => ({ name: 'deploy-hook', url: 'https://deploy.example.com/in', events: ['content.upload'], secret: 'a-very-long-signing-secret', ...fields })

describe('mock webhooks API', () => {
  beforeAll(() => {
    localStorage.setItem('ws.mock.session', 'admin')
    vi.useFakeTimers({ toFake: ['Date'] })
  })
  afterAll(() => {
    vi.useRealTimers()
    localStorage.clear()
  })

  it('lists the seeded webhooks with their last delivery and the event catalogue', async () => {
    const { status, json } = await api<WebhookList>('GET', '/webhooks')
    expect(status).toBe(200)
    expect(json.items.map((w) => w.name)).toEqual(['chat-notifications', 'ci-release-builds', 'inventory-sync'])
    expect(json.events).toContain('content.upload')
    const chat = json.items.find((w) => w.name === 'chat-notifications')!
    expect(chat.last_delivery).toMatchObject({ status: 'pending', attempts: 2, status_code: 503 })
    const inventory = json.items.find((w) => w.name === 'inventory-sync')!
    expect(inventory).toMatchObject({ enabled: false, secret_set: true })
    expect(inventory.last_delivery?.status).toBe('dead')
    expect(json.items.find((w) => w.name === 'ci-release-builds')!.header_names).toEqual(['Authorization'])
  })

  it('validates like the server and never returns secrets', async () => {
    const noSecret = await api<Problem>('POST', '/webhooks', write({ secret: null }))
    expect(noSecret.status).toBe(422)
    expect(noSecret.json.errors?.[0]?.field).toBe('secret')
    const local = await api<Problem>('POST', '/webhooks', write({ url: 'http://192.168.1.20/in' }))
    expect(local.json.errors?.[0]?.code).toBe('private_address')
    const taken = await api<Problem>('POST', '/webhooks', write({ name: 'CI-release-builds' }))
    expect(taken.status).toBe(409)
    const created = await api<Webhook>('POST', '/webhooks', write({ headers: { 'X-Token': 'abc' } }))
    expect(created.status).toBe(201)
    expect(JSON.stringify(created.json)).not.toContain('a-very-long-signing-secret')
    expect(JSON.stringify(created.json)).not.toContain('abc')
    expect(created.json.header_names).toEqual(['X-Token'])
    const kept = await api<Webhook>('PUT', `/webhooks/${created.json.id}`, write({ secret: null, headers: null, enabled: false }))
    expect(kept.json).toMatchObject({ enabled: false, header_names: ['X-Token'] })
    expect((await api('DELETE', `/webhooks/${created.json.id}`)).status).toBe(204)
    expect((await api('GET', `/webhooks/${created.json.id}`)).status).toBe(404)
  })

  it('sends a test event that is delivered a moment later, and redelivers', async () => {
    const created = await api<Webhook>('POST', '/webhooks', write({ name: 'ping-me' }))
    const id = created.json.id
    const sent = await api<WebhookDeliveryAccepted>('POST', `/webhooks/${id}/test`)
    expect(sent.status).toBe(202)
    const queued = await api<WebhookDelivery>('GET', `/webhooks/${id}/deliveries/${sent.json.delivery_id}`)
    expect(queued.json).toMatchObject({ status: 'pending', attempts: 0, event: 'webhook.ping', trigger: 'test' })
    vi.setSystemTime(Date.now() + 2000)
    const done = await api<WebhookDelivery>('GET', `/webhooks/${id}/deliveries/${sent.json.delivery_id}`)
    expect(done.json).toMatchObject({ status: 'succeeded', attempts: 1, status_code: 200 })
    expect(done.json.request_headers['Waystation-Event']).toBe('webhook.ping')
    expect(JSON.parse(done.json.request_body).type).toBe('webhook.ping')

    const again = await api<WebhookDeliveryAccepted>('POST', `/webhooks/${id}/deliveries/${sent.json.delivery_id}/redeliver`)
    const redelivery = await api<WebhookDelivery>('GET', `/webhooks/${id}/deliveries/${again.json.delivery_id}`)
    expect(redelivery.json).toMatchObject({ trigger: 'redeliver', redelivery_of: sent.json.delivery_id, event_id: done.json.event_id })
    const log = await api<WebhookDeliveryList>('GET', `/webhooks/${id}/deliveries`)
    expect(log.json.items.map((d) => d.id)).toEqual([again.json.delivery_id, sent.json.delivery_id])
  })

  it('times out against a failing receiver and filters the log by status', async () => {
    const created = await api<Webhook>('POST', '/webhooks', write({ name: 'broken', url: 'https://fail.example.com/in' }))
    const sent = await api<WebhookDeliveryAccepted>('POST', `/webhooks/${created.json.id}/test`)
    vi.setSystemTime(Date.now() + 2000)
    const dead = await api<WebhookDelivery>('GET', `/webhooks/${created.json.id}/deliveries/${sent.json.delivery_id}`)
    expect(dead.json).toMatchObject({ status: 'dead', error: 'timed out after 10 s' })
    const onlyDead = await api<WebhookDeliveryList>('GET', `/webhooks/${created.json.id}/deliveries?status=succeeded`)
    expect(onlyDead.json.items).toEqual([])
  })
})
