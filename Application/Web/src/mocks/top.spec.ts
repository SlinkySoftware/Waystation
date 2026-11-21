// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { seedRepositories } from './fixtures/repositories'
import { catalog, digestHex, digestUsage, repoDedup, storageHistory, topArtifacts, topRepositories } from './top'
import type { MockWorld } from './world'

const repos = seedRepositories()

describe('mock top artifacts', () => {
  it('covers docker, npm, apt, yum and raw with real public names', () => {
    const formats = new Set(catalog().map((e) => e.format))
    expect([...formats].sort()).toEqual(['apt', 'docker', 'npm', 'raw', 'yum'])
    expect(catalog().some((e) => e.display === 'library/nginx:1.29.3')).toBe(true)
    expect(catalog().some((e) => e.display === 'lodash@4.17.21')).toBe(true)
  })

  it('orders by the requested measure and respects the limit and the visible repositories', () => {
    const top = topArtifacts(repos, 'downloads', '7d', null, 5)
    expect(top.items).toHaveLength(5)
    const counts = top.items.map((i) => i.downloads)
    expect([...counts].sort((a, b) => b - a)).toEqual(counts)
    const largest = topArtifacts(repos, 'size', '7d', null, 10).items.map((i) => i.size_bytes)
    expect([...largest].sort((a, b) => b - a)).toEqual(largest)
    const recent = topArtifacts(repos, 'recent', 'all', null, 10).items.map((i) => Date.parse(i.created_at))
    expect([...recent].sort((a, b) => b - a)).toEqual(recent)
    expect(topArtifacts(repos.filter((r) => r.name === 'npm'), 'downloads', 'all', null, 50).items.every((i) => i.repository === 'npm')).toBe(true)
    expect(topArtifacts(repos, 'downloads', 'all', 'debian', 50).items.every((i) => i.repository === 'debian')).toBe(true)
  })

  it('counts fewer downloads in a shorter window, bytes = downloads x size', () => {
    const all = topArtifacts(repos, 'downloads', 'all', 'npm', 100).items
    const day = topArtifacts(repos, 'downloads', '24h', 'npm', 100).items
    expect(day.reduce((a, i) => a + i.downloads, 0)).toBeLessThan(all.reduce((a, i) => a + i.downloads, 0))
    for (const i of day) expect(i.bytes_served).toBe(i.downloads * i.size_bytes)
  })
})

describe('mock top repositories and storage history', () => {
  it('ranks repositories by requests', () => {
    const top = topRepositories(repos, '24h', 'requests', 3)
    expect(top.items.length).toBeLessThanOrEqual(3)
    expect(top.items[0]!.requests).toBeGreaterThanOrEqual(top.items[top.items.length - 1]!.requests)
  })

  it('has one value per bucket, null before the first snapshot', () => {
    const world = { storageUsed: 1e12 } as MockWorld
    const year = storageHistory(world, repos, '1y')
    expect(year.step_s).toBe(86400)
    expect(year.timestamps).toHaveLength(365)
    expect(year.used_bytes[0]).toBeNull()
    expect(year.used_bytes[364]).toBeGreaterThan(0)
    const week = storageHistory(world, repos, '7d')
    expect(week.used_bytes.every((v) => v !== null)).toBe(true)
    expect(week.stored_bytes).toHaveLength(week.timestamps.length)
  })

  it('reports per-repository de-duplication savings below the stored bytes', () => {
    for (const r of repos) expect(repoDedup(r).dedup_saved_bytes).toBeLessThanOrEqual(r.stats.size_bytes)
  })
})

describe('mock digest usage', () => {
  it('accepts sha256:<hex> and bare hex, finds the asset', () => {
    const e = catalog().find((x) => x.format === 'docker')!
    expect(digestHex(`sha256:${e.sha256}`)).toBe(e.sha256)
    expect(digestHex('nope')).toBeNull()
    const usage = digestUsage(repos, e.sha256!)
    expect(usage.size).toBe(e.size_bytes)
    expect(usage.items.some((i) => i.kind === 'asset' && i.path === e.path)).toBe(true)
    expect(usage.items.some((i) => i.kind === 'docker_layer')).toBe(true)
    expect(digestUsage([], e.sha256!).items).toEqual([])
  })
})
