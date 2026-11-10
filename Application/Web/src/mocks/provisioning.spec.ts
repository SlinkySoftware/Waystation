// SPDX-License-Identifier: GPL-3.0-or-later
import { getResponse } from 'msw'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import type { ProvisioningPlan, ProvisioningStatus, Repository } from '@/api/types'

import { handlers } from './handlers'

const CSRF = 'mock-csrf-7f3a9c'
async function api<T>(method: string, path: string): Promise<{ status: number; json: T }> {
  const request = new Request(new URL(`/api/v1${path}`, location.origin), { method, headers: { 'X-CSRF-Token': CSRF } })
  const response = (await getResponse(handlers, request))!
  return { status: response.status, json: (await response.json()) as T }
}

describe('mock provisioning API', () => {
  beforeAll(() => localStorage.setItem('ws.mock.session', 'admin'))
  afterAll(() => localStorage.clear())

  it('reports a configured file, plans changes with redacted secrets, and applies them', async () => {
    const status = await api<ProvisioningStatus>('GET', '/provisioning')
    expect(status.json).toMatchObject({ configured: true, file: '/etc/waystation/provisioning.yaml' })

    const plan = await api<ProvisioningPlan>('POST', '/provisioning/plan')
    expect(plan.json.valid).toBe(true)
    expect(plan.json.summary).toMatchObject({ create: 1, update: 2 })
    const hook = plan.json.items?.find((i) => i.kind === 'webhook')
    expect(hook?.changes?.[0]).toMatchObject({ field: 'secret', redacted: true })

    const applied = await api<ProvisioningPlan>('POST', '/provisioning/apply')
    expect(applied.json.applied).toBe(true)
    expect(applied.json.items?.filter((i) => i.outcome === 'applied')).toHaveLength(3)
    const again = await api<ProvisioningPlan>('POST', '/provisioning/plan')
    expect(again.json.summary).toMatchObject({ create: 0, update: 0, prune: 0 })
    expect((await api<ProvisioningStatus>('GET', '/provisioning')).json.last_apply?.by).toBe('admin')
  })

  it('marks the repositories the file declares as managed', async () => {
    const repo = await api<Repository>('GET', '/repositories/dockerhub-proxy')
    expect(repo.json.managed_by).toBe('provisioning')
    expect((await api<Repository>('GET', '/repositories/debian')).json.managed_by).toBeNull()
  })
})
