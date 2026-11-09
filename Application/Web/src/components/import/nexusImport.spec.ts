// SPDX-License-Identifier: GPL-3.0-or-later
import { getResponse } from 'msw'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import type { NexusImportItem, NexusImportReport, Problem } from '@/api/types'
import { handlers } from '@/mocks/handlers'

import { creatable, dockerCandidates, emptyModel, rowState, target, toApply, toRequest } from './nexusImport'

const CSRF = 'mock-csrf-7f3a9c'
async function post<T>(path: string, body: unknown): Promise<{ status: number; json: T }> {
  const request = new Request(new URL(`/api/v1${path}`, location.origin), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
    body: JSON.stringify(body),
  })
  const response = (await getResponse(handlers, request))!
  return { status: response.status, json: (await response.json()) as T }
}

const item = (over: Partial<NexusImportItem>): NexusImportItem => ({
  name: 'debian',
  nexus_format: 'apt',
  nexus_type: 'proxy',
  format: 'apt',
  type: 'proxy',
  action: 'create',
  outcome: null,
  reason: null,
  repository: null,
  differences: [],
  notes: [],
  ...over,
})

describe('nexus import model', () => {
  it('builds requests without keeping blank optional fields', () => {
    const m = { ...emptyModel(), url: ' https://nexus.example.com/ ', password: 'pw', caPem: '  ' }
    expect(toRequest(m)).toEqual({
      url: 'https://nexus.example.com/',
      username: 'admin',
      password: 'pw',
      allow_private: false,
      ca_pem: null,
      docker_aliases: true,
      default_docker_repo: null,
    })
    expect(toApply(m, ['debian']).repositories).toEqual(['debian'])
  })

  it('shows the outcome once applied, else the planned action, never by colour alone', () => {
    expect(rowState(item({}))).toMatchObject({ label: 'Create', cls: 'tone-hit' })
    expect(rowState(item({ outcome: 'failed' }))).toMatchObject({ label: 'Failed', cls: 'tone-blocked' })
    expect(rowState(item({ action: 'invalid' })).icon).toBeTruthy()
  })

  it('lists what can be created and the docker candidates', () => {
    const report = {
      items: [
        item({}),
        item({ name: 'hub', format: 'docker', repository: { name: 'hub', format: 'docker', type: 'proxy', online: true, proxy: { upstream_url: 'https://registry-1.docker.io' } } as NexusImportItem['repository'] }),
        item({ name: 'docker-hosted', format: 'docker', type: 'hosted' }),
        item({ name: 'old', action: 'exists' }),
      ],
    } as NexusImportReport
    expect(creatable(report)).toEqual(['debian', 'hub', 'docker-hosted'])
    expect(dockerCandidates(report)).toEqual(['hub'])
    expect(target(report.items[1]!)).toBe('https://registry-1.docker.io')
  })
})

describe('mock nexus import API', () => {
  beforeAll(() => localStorage.setItem('ws.mock.session', 'admin'))
  afterAll(() => localStorage.clear())
  const body = { url: 'https://nexus.example.com', username: 'admin', password: 'pw' }

  it('plans against the demo repositories and applies the selection', async () => {
    const preview = await post<NexusImportReport>('/import/nexus/preview', body)
    expect(preview.status).toBe(200)
    const actions = Object.fromEntries(preview.json.items.map((i) => [i.name, i.action]))
    expect(actions['debian']).toBe('exists')
    expect(actions['quay-proxy']).toBe('create')
    expect(actions['maven-central']).toBe('unsupported')
    const applied = await post<NexusImportReport>('/import/nexus/apply', { ...body, repositories: ['quay-proxy'] })
    expect(applied.json.applied).toBe(true)
    expect(applied.json.items.find((i) => i.name === 'quay-proxy')?.outcome).toBe('created')
    expect(applied.json.items.find((i) => i.name === 'ubuntu-ports')?.outcome).toBe('not_selected')
    const again = await post<NexusImportReport>('/import/nexus/preview', body)
    expect(again.json.items.find((i) => i.name === 'quay-proxy')?.action).toBe('exists')
  })

  it('refuses wrong credentials and a bad URL like the server', async () => {
    const refused = await post<Problem>('/import/nexus/preview', { ...body, password: 'wrong' })
    expect(refused.status).toBe(502)
    expect(refused.json.detail).toContain('refused the credentials')
    const bad = await post<Problem>('/import/nexus/preview', { ...body, url: 'nexus' })
    expect(bad.status).toBe(422)
    expect(bad.json.errors?.[0]?.field).toBe('url')
  })
})
