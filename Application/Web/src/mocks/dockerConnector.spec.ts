// SPDX-License-Identifier: GPL-3.0-or-later
// Docker dedicated port connectors in the mock API (T-P6-DOCKERC): validation, clone and Set me up mirror the server.
import { getResponse } from 'msw'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import type { DockerOptions, Problem, Repository } from '@/api/types'

import { handlers } from './handlers'
import { setupGuide } from './setup'

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

const hosted = (name: string, port: number | null) => ({
  name,
  format: 'docker',
  type: 'hosted',
  online: true,
  hosted: { write_policy: 'allow_redeploy' },
  format_options: { aliases: [], docker_hub: false, default_repo: false, connector_port: port },
})
const snippets = (repo: Repository) => setupGuide(repo).clients.flatMap((c) => c.snippets)

describe('mock docker dedicated port connector', () => {
  beforeAll(() => localStorage.setItem('ws.mock.session', 'admin'))
  afterAll(() => localStorage.clear())

  it('rejects a port out of range or taken by another repository', async () => {
    const low = await api<Problem>('POST', '/repositories', hosted('docker-low', 80))
    expect(low.status).toBe(422)
    expect(low.json.errors).toContainEqual(expect.objectContaining({ field: 'format_options.connector_port', code: 'range' }))
    const taken = await api<Problem>('POST', '/repositories', hosted('docker-taken', 5001))
    expect(taken.status).toBe(422)
    expect(taken.json.errors).toContainEqual({ field: 'format_options.connector_port', message: 'is already the connector port of ghcr-proxy', code: 'taken' })
  })

  it('stores a free port, keeps it on update, and a clone does not copy it', async () => {
    const created = await api<Repository>('POST', '/repositories', hosted('docker-releases', 5002))
    expect(created.status).toBe(201)
    expect((created.json.format_options as DockerOptions).connector_port).toBe(5002)
    // Saving the same repository again is not a clash with itself.
    const { name: _name, ...update } = hosted('docker-releases', 5002)
    expect((await api<Repository>('PUT', '/repositories/docker-releases', update)).status).toBe(200)
    const clone = await api<Repository>('POST', '/repositories/docker-releases/clone', { name: 'docker-releases-copy' })
    expect(clone.status).toBe(201)
    expect((clone.json.format_options as DockerOptions).connector_port).toBeNull()
  })

  it('adds the connector snippet and note only when a port is set', async () => {
    const ghcr = (await api<Repository>('GET', '/repositories/ghcr-proxy')).json
    const connector = snippets(ghcr).find((s) => s.id === 'docker-connector')!
    expect(connector.title).toBe('Pull through the dedicated port 5001')
    expect(connector.content).toBe('docker pull registry.example.com:5001/org/image:tag')
    expect(connector.description).toBe('No repository prefix on this port. Sign in with docker login registry.example.com:5001 when anonymous pulls are not allowed.')
    expect(setupGuide(ghcr).notes).toContain('registry.example.com:5001/… (dedicated port) also routes here.')

    const hub = (await api<Repository>('GET', '/repositories/dockerhub-proxy')).json
    expect(snippets(hub).some((s) => s.id === 'docker-connector')).toBe(false)
    expect((setupGuide(hub).notes ?? []).join(' ')).not.toContain('dedicated port')

    const releases = (await api<Repository>('GET', '/repositories/docker-releases')).json
    const ids = snippets(releases).map((s) => s.id)
    expect(ids.indexOf('docker-connector')).toBe(ids.indexOf('docker-push') + 1)
    const push = snippets(releases).find((s) => s.id === 'docker-connector')!
    expect(push.title).toBe('Push and pull through the dedicated port 5002')
    expect(push.content).toBe('docker login registry.example.com:5002\ndocker tag myapp:1.0 registry.example.com:5002/myapp:1.0\ndocker push registry.example.com:5002/myapp:1.0')
  })
})
