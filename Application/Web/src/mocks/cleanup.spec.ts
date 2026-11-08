// SPDX-License-Identifier: GPL-3.0-or-later
import { getResponse } from 'msw'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import type { CleanupCriteria, CleanupPolicyList, CleanupPreview, CleanupRunList, GcStatus, Problem, Repository } from '@/api/types'

import { nextRun, validatePolicy } from './cleanup'
import { evaluate, inventory, olderVersion, validateCriteria } from './fixtures/cleanup'
import { seedRepositories } from './fixtures/repositories'
import { handlers } from './handlers'

const NOW = Date.UTC(2026, 10, 9, 12, 0, 0)
const repos = seedRepositories()
const repo = (name: string) => repos.find((r) => r.name === name)!
const crit = (partial: Partial<CleanupCriteria>): CleanupCriteria => ({
  last_downloaded_older_than_days: null,
  published_older_than_days: null,
  keep_latest_n_versions: null,
  name_include: null,
  name_exclude: null,
  version_include: null,
  version_exclude: null,
  prerelease_only: false,
  untagged_older_than_days: null,
  ...partial,
})
const count = (c: Partial<CleanupCriteria>, list: Repository[]) => evaluate(crit(c), list, new Set(), NOW).candidates.length

describe('mock cleanup inventory and evaluator', () => {
  it('derives older versions with the same shape', () => {
    expect(olderVersion('1.29.3', 2)).toBe('1.29.1')
    expect(olderVersion('v3.5.4', 1)).toBe('v3.5.3')
    expect(olderVersion('2.41-12', 3)).toBe('2.41-9')
    expect(olderVersion('1.29.3', 8)).toBe('1.28.5')
    expect(olderVersion('19.2.0', 1)).toBe('19.1.9')
  })

  it('is deterministic and changes with the criteria', () => {
    const npm = [repo('npm')]
    expect(count({ last_downloaded_older_than_days: 90 }, npm)).toBe(count({ last_downloaded_older_than_days: 90 }, npm))
    expect(count({ last_downloaded_older_than_days: 30 }, npm)).toBeGreaterThan(count({ last_downloaded_older_than_days: 365 }, npm))
    // AND: adding a criterion never selects more.
    expect(count({ last_downloaded_older_than_days: 30, keep_latest_n_versions: 3 }, npm)).toBeLessThanOrEqual(count({ last_downloaded_older_than_days: 30 }, npm))
    expect(count({ last_downloaded_older_than_days: 30, name_exclude: '.' }, npm)).toBe(0)
  })

  it('keeps the newest N versions of every component', () => {
    const result = evaluate(crit({ keep_latest_n_versions: 2 }), [repo('npm')], new Set(), NOW)
    const items = inventory(repo('npm'), NOW)
    for (const name of new Set(items.map((i) => i.name))) {
      const versions = items.filter((i) => i.name === name).length
      expect(result.candidates.filter((c) => c.name === name)).toHaveLength(Math.max(0, versions - 2))
    }
  })

  it('selects docker untagged manifests (with their layers) and cascades removed tags', () => {
    const untagged = evaluate(crit({ untagged_older_than_days: 1 }), [repo('dockerhub-proxy')], new Set(), NOW).candidates
    expect(untagged.length).toBeGreaterThan(0)
    expect(new Set(untagged.map((c) => c.kind))).toEqual(new Set(['manifest', 'layer']))
    const tags = evaluate(crit({ keep_latest_n_versions: 4 }), [repo('dockerhub-proxy')], new Set(), NOW).candidates
    expect(tags.some((c) => c.kind === 'tag' && c.size === 0)).toBe(true)
    expect(tags.some((c) => c.kind === 'layer' && c.reason.startsWith('only used by tag'))).toBe(true)
    // Untagged never applies to other formats.
    expect(count({ untagged_older_than_days: 1 }, [repo('npm')])).toBe(0)
  })

  it('validates criteria like the server', () => {
    expect(validateCriteria(crit({}), 'npm').map((e) => e.field)).toEqual(['criteria'])
    expect(validateCriteria(crit({ name_exclude: 'x' }), 'npm').map((e) => e.field)).toEqual(['criteria'])
    expect(validateCriteria(crit({ untagged_older_than_days: 14 }), 'npm').map((e) => e.field)).toEqual(['criteria.untagged_older_than_days'])
    expect(validateCriteria(crit({ keep_latest_n_versions: 0 }), 'docker').map((e) => e.field)).toContain('criteria.keep_latest_n_versions')
    expect(validateCriteria(crit({ name_include: '(' }), 'docker').map((e) => e.field)).toEqual(['criteria.name_include'])
    expect(validatePolicy({ name: 'x', format: 'npm', criteria: crit({ prerelease_only: true }), schedule: 'nightly', enabled: true }).map((e) => e.field)).toEqual(['name', 'schedule'])
  })

  it('computes the next scheduled run in UTC', () => {
    expect(nextRun('0 3 * * *', NOW)).toBe('2026-11-10T03:00:00.000Z')
    expect(nextRun('0 3 * * 0', NOW)).toBe('2026-11-15T03:00:00.000Z')
    expect(nextRun('@daily', NOW)).toBe('2026-11-10T00:00:00.000Z')
  })
})

describe('mock cleanup API', () => {
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

  beforeAll(() => {
    localStorage.setItem('ws.mock.session', 'admin')
    vi.useFakeTimers({ toFake: ['Date'] })
  })
  afterAll(() => {
    vi.useRealTimers()
    localStorage.clear()
  })

  it('lists the seeded policies with attachments and last runs', async () => {
    const { json } = await api<CleanupPolicyList>('GET', '/cleanup-policies')
    const byName = new Map(json.items.map((p) => [p.name, p]))
    expect(byName.get('docker-untagged-14d')?.repositories).toEqual(['dockerhub-proxy', 'ghcr-proxy'])
    expect(byName.get('apt-old-debs-180d')?.last_run?.status).toBe('running')
    expect(byName.get('all-unused-365d')?.repositories).toEqual([])
    expect(byName.get('all-unused-365d')?.next_run_at).toBeNull()
  })

  it('answers previews with totals only for limit 0, pages otherwise, and 422 with field errors', async () => {
    const draft = { format: 'npm', criteria: crit({ last_downloaded_older_than_days: 30 }) }
    const totals = await api<CleanupPreview>('POST', '/cleanup-policies/preview', { ...draft, limit: 0 })
    expect(totals.json.count).toBeGreaterThan(10)
    expect(totals.json.items).toEqual([])
    expect(totals.json.repositories.map((r) => r.name)).toEqual(['npm'])
    const page1 = await api<CleanupPreview>('POST', '/cleanup-policies/preview', { ...draft, limit: 5 })
    const page2 = await api<CleanupPreview>('POST', '/cleanup-policies/preview', { ...draft, limit: 5, cursor: page1.json.next_cursor })
    expect(page1.json.items).toHaveLength(5)
    expect(page2.json.items[0]).not.toEqual(page1.json.items[0])
    const bad = await api<Problem>('POST', '/cleanup-policies/preview', { format: 'npm', criteria: crit({ untagged_older_than_days: 3 }), limit: 0 })
    expect(bad.status).toBe(422)
    expect(bad.json.errors?.[0]?.field).toBe('criteria.untagged_older_than_days')
    expect((await api('POST', '/cleanup-policies/preview', { ...draft, repositories: ['nope'], limit: 0 })).status).toBe(404)
  })

  it('runs a policy: running, merged on repeat, then removes exactly what the preview showed', async () => {
    const { json: list } = await api<CleanupPolicyList>('GET', '/cleanup-policies')
    const policy = list.items.find((p) => p.name === 'npm-unused-90d')!
    const before = await api<CleanupPreview>('POST', '/cleanup-policies/preview', { format: 'npm', criteria: policy.criteria, policy_id: policy.id, limit: 0 })
    expect((await api<{ status: string }>('POST', `/cleanup-policies/${policy.id}/run`)).json.status).toBe('enqueued')
    expect((await api<{ status: string }>('POST', `/cleanup-policies/${policy.id}/run`)).json.status).toBe('merged')
    expect((await api<CleanupRunList>('GET', `/cleanup-policies/${policy.id}/runs`)).json.items[0]?.status).toBe('running')
    vi.setSystemTime(Date.now() + 10_000)
    const run = (await api<CleanupRunList>('GET', `/cleanup-policies/${policy.id}/runs`)).json.items[0]!
    expect(run).toMatchObject({ status: 'succeeded', trigger: 'manual', removed: before.json.count, bytes: before.json.bytes })
    const after = await api<CleanupPreview>('POST', '/cleanup-policies/preview', { format: 'npm', criteria: policy.criteria, policy_id: policy.id, limit: 0 })
    expect(after.json.count).toBe(0)
  })

  it('attaches only compatible policies, in order', async () => {
    const { json: list } = await api<CleanupPolicyList>('GET', '/cleanup-policies')
    const npm = list.items.find((p) => p.format === 'npm')!
    const all = list.items.find((p) => p.format === '*')!
    const wrong = await api<Problem>('PUT', '/repositories/dockerhub-proxy/cleanup', { policy_ids: [npm.id] })
    expect(wrong.status).toBe(422)
    expect(wrong.json.errors?.[0]).toMatchObject({ field: 'policy_ids.0', code: 'format' })
    const ok = await api<{ policies: { id: string }[] }>('PUT', '/repositories/debian/cleanup', { policy_ids: [all.id, list.items.find((p) => p.format === 'apt')!.id] })
    expect(ok.json.policies.map((p) => p.id)[0]).toBe(all.id)
  })

  it('creates, rejects duplicates and deletes policies', async () => {
    const body = { name: 'docker-nightlies-7d', format: 'docker', criteria: crit({ version_include: '^nightly-', published_older_than_days: 7 }), schedule: '0 3 * * *', enabled: true }
    const created = await api<{ id: string; next_run_at: string | null }>('POST', '/cleanup-policies', body)
    expect(created.status).toBe(201)
    expect(created.json.next_run_at).not.toBeNull()
    expect((await api('POST', '/cleanup-policies', { ...body, name: 'DOCKER-nightlies-7d' })).status).toBe(409)
    expect((await api('POST', '/cleanup-policies', { ...body, name: 'other', criteria: crit({}) })).status).toBe(422)
    expect((await api('DELETE', `/cleanup-policies/${created.json.id}`)).status).toBe(204)
    expect((await api('GET', `/cleanup-policies/${created.json.id}`)).status).toBe(404)
  })

  it('runs blob GC: frees what is past the grace period', async () => {
    // Let the seeded running cleanup finish first (it adds to what GC waits on).
    vi.setSystemTime(Date.now() + 60_000)
    await api('GET', '/cleanup-policies')
    const before = (await api<GcStatus>('GET', '/maintenance/gc')).json
    expect(before.grace_hours).toBe(24)
    expect((await api<{ status: string }>('POST', '/maintenance/gc/run')).json.status).toBe('enqueued')
    vi.setSystemTime(Date.now() + 10_000)
    const after = (await api<GcStatus>('GET', '/maintenance/gc')).json
    expect(after.runs[0]).toMatchObject({ status: 'succeeded', trigger: 'manual' })
    expect(after.runs[0]!.freed_bytes).toBeGreaterThan(0)
    expect(after.pending_bytes).toBe(before.pending_bytes - after.runs[0]!.freed_bytes)
  })
})
