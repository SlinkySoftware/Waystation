// SPDX-License-Identifier: GPL-3.0-or-later
import { getResponse } from 'msw'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import type { JobAccepted, JobRun, PrefetchStatus, Problem, Repository, RepositoryWrite, ScheduledTaskList } from '@/api/types'

import { handlers } from './handlers'
import { repositoryId } from './prefetch'

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

/** A repository as the settings form writes it back (the read shape is close enough for the mock). */
function writeOf(repo: Repository, prefetch: NonNullable<RepositoryWrite['proxy']>['prefetch']): RepositoryWrite {
  const { auth: _auth, ...proxy } = repo.proxy!
  return { name: repo.name, format: repo.format, type: 'proxy', online: repo.online, description: repo.description ?? '', proxy: { ...proxy, auth: { kind: 'none' }, prefetch }, format_options: repo.format_options } as RepositoryWrite
}

describe('mock prefetch API', () => {
  beforeAll(() => {
    localStorage.setItem('ws.mock.session', 'admin')
    vi.useFakeTimers({ toFake: ['Date'] })
  })
  afterAll(() => {
    vi.useRealTimers()
    localStorage.clear()
  })

  it('seeds the Docker Hub and npm proxies with prefetch and reports their status', async () => {
    const hub = await api<Repository>('GET', '/repositories/dockerhub-proxy')
    expect(hub.json.proxy?.prefetch).toMatchObject({ refresh_popular: true, schedule: '0 3 * * *' })
    expect(hub.json.proxy?.prefetch?.warm).toContain('library/nginx:1.27')
    const status = await api<PrefetchStatus>('GET', '/repositories/dockerhub-proxy/prefetch')
    expect(status.status).toBe(200)
    expect(status.json).toMatchObject({ warm_supported: true, refresh_supported: true, warm_items: 4, refresh_popular: true, schedule: '0 3 * * *' })
    expect(status.json.next_run_at).not.toBeNull()
    expect(status.json.last_run).toMatchObject({ status: 'succeeded', trigger: 'schedule', items: 4 })
    const npm = await api<PrefetchStatus>('GET', '/repositories/npm/prefetch')
    expect(npm.json).toMatchObject({ schedule: null, next_run_at: null, last_run: { trigger: 'manual', failed: 1 } })
    // A proxy of a format without prefetch: neither supported, defaults.
    expect((await api<PrefetchStatus>('GET', '/repositories/debian/prefetch')).json).toMatchObject({ warm_supported: false, refresh_supported: false, warm_items: 0, last_run: null })
    expect((await api<Problem>('GET', '/repositories/nope/prefetch')).status).toBe(404)
  })

  it('refuses non-proxy repositories and an empty warm list (409)', async () => {
    const created = await api<Repository>('POST', '/repositories', { name: 'raw-hosted-pf', format: 'raw', type: 'hosted', online: true, hosted: { write_policy: 'allow_redeploy' } })
    expect(created.status).toBe(201)
    expect((await api<Problem>('GET', '/repositories/raw-hosted-pf/prefetch')).status).toBe(409)
    expect((await api<Problem>('POST', '/repositories/raw-hosted-pf/prefetch/run')).status).toBe(409)
    const ghcr = await api<Problem>('POST', '/repositories/ghcr-proxy/prefetch/run')
    expect(ghcr.status).toBe(409)
    expect(ghcr.json.detail).toContain('no warm list')
  })

  it('warms now, joins a run in progress, and the run finishes and shows on the Tasks page', async () => {
    const first = await api<JobAccepted>('POST', '/repositories/npm/prefetch/run')
    expect(first.status).toBe(202)
    expect(first.json.status).toBe('enqueued')
    const again = await api<JobAccepted>('POST', '/repositories/npm/prefetch/run')
    expect(again.json).toEqual({ job_id: first.json.job_id, status: 'merged' })
    const running = await api<PrefetchStatus>('GET', '/repositories/npm/prefetch')
    expect(running.json.last_run).toMatchObject({ status: 'running', trigger: 'manual' })
    const runId = running.json.last_run!.id
    const run = await api<JobRun>('GET', `/tasks/runs/${runId}`)
    expect(run.json).toMatchObject({ kind: 'prefetch.warm', queue: 'prefetch', status: 'running' })
    vi.setSystemTime(Date.now() + 10_000)
    const done = await api<PrefetchStatus>('GET', '/repositories/npm/prefetch')
    expect(done.json.last_run).toMatchObject({ id: runId, status: 'succeeded', items: 4 })
    expect(done.json.last_run!.files).toBeGreaterThan(0)
    const log = await api<JobRun>('GET', `/tasks/runs/${runId}`)
    expect(log.json.log.join('\n')).toContain('lodash@4.17.21')
  })

  it('lists a scheduled warm list as a task owned by its repository (schedule edited there)', async () => {
    const { json } = await api<ScheduledTaskList>('GET', '/tasks/scheduled')
    const id = `prefetch:${repositoryId('dockerhub-proxy')}`
    const task = json.items.find((t) => t.id === id)!
    expect(task).toMatchObject({ kind: 'prefetch.warm', queue: 'prefetch', schedule: '0 3 * * *', owner: { kind: 'repository', id: repositoryId('dockerhub-proxy'), name: 'dockerhub-proxy' } })
    expect(task.last_run?.kind).toBe('prefetch.warm')
    // npm has no schedule: no task.
    expect(json.items.some((t) => t.id === `prefetch:${repositoryId('npm')}`)).toBe(false)
    const refused = await api<Problem>('PATCH', `/tasks/scheduled/${id}`, { schedule: '@daily' })
    expect(refused.status).toBe(409)
    expect(refused.json.detail).toContain('prefetch settings')
    expect((await api<JobAccepted>('POST', `/tasks/scheduled/${id}/run`)).status).toBe(202)
  })

  it('persists prefetch on save and validates it like the server', async () => {
    const repo = (await api<Repository>('GET', '/repositories/raw-releases')).json
    const saved = await api<Repository>('PUT', '/repositories/raw-releases', writeOf(repo, { refresh_popular: true, warm: ['cli/releases/download/v2.62.0/gh_2.62.0_linux_amd64.tar.gz'], schedule: ' @every  6h ' }))
    expect(saved.status).toBe(200)
    expect(saved.json.proxy?.prefetch).toEqual({ refresh_popular: true, warm: ['cli/releases/download/v2.62.0/gh_2.62.0_linux_amd64.tar.gz'], schedule: '@every 6h' })
    expect((await api<Repository>('GET', '/repositories/raw-releases')).json.proxy?.prefetch?.schedule).toBe('@every 6h')
    const { json } = await api<ScheduledTaskList>('GET', '/tasks/scheduled')
    expect(json.items.find((t) => t.id === `prefetch:${repositoryId('raw-releases')}`)?.schedule).toBe('@every 6h')

    const bad = await api<Problem>('PUT', '/repositories/raw-releases', writeOf(repo, { refresh_popular: false, warm: ['ok/file.txt', '../etc/passwd'], schedule: '@every 5m' }))
    expect(bad.status).toBe(422)
    expect(bad.json.errors?.map((e) => e.field)).toEqual(['proxy.prefetch.warm.1', 'proxy.prefetch.schedule'])

    const debian = (await api<Repository>('GET', '/repositories/debian')).json
    const unsupported = await api<Problem>('PUT', '/repositories/debian', writeOf(debian, { refresh_popular: true, warm: [] }))
    expect(unsupported.json.errors?.map((e) => e.field)).toEqual(['proxy.prefetch.refresh_popular'])
  })
})
