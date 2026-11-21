// SPDX-License-Identifier: GPL-3.0-or-later
// Top-N, storage history and digest "where used" for the mock world (GET /stats/top/*, /stats/storage/history,
// /digests/{digest}/usage). A deterministic catalog of components built from the same fixtures the browse mocks use
// (real public image, package and release names), with seeded download counts and cache times.
import type { DigestUsage, DigestUse, Format, HistoryRange, Repository, StatsRange, StorageHistory, TopArtifact, TopArtifacts, TopRepositories, TopWindow } from '@/api/types'

import { aptInstallPlan } from './fixtures/apt'
import { DOCKER_IMAGES } from './fixtures/docker'
import { NPM_PACKAGES, tarballPath } from './fixtures/npm'
import { RAW_FILES } from './fixtures/raw'
import { yumPackages } from './fixtures/yum'
import { fakeSha256, hash, KiB, MiB, rng } from './rng'
import { RANGES } from './stats'
import type { MockWorld } from './world'

const HOUR = 3_600_000
const DAY = 24 * HOUR

export type TopBy = TopArtifacts['by']
export type TopRepoBy = TopRepositories['by']

interface Entry extends TopArtifact {
  /** Downloads since cached (the `all` window). */
  total: number
}

const WINDOW_SHARE: Record<TopWindow, number> = { '24h': 0.035, '7d': 0.21, '30d': 0.64, all: 1 }

function entry(repository: string, format: Format, namespace: string | null, name: string, version: string | null, display: string, size: number, path: string, popularity: number): Entry {
  const next = rng(hash(`top:${repository}:${display}`))
  const total = Math.round(popularity * (0.4 + next() * 1.6))
  const created = Date.now() - Math.round(next() * 120 * DAY) - 5 * 60_000
  const last = total ? Date.now() - Math.round(next() * 6 * HOUR) : null
  return {
    component_id: `cmp-${hash(`${repository}:${display}`).toString(36)}`,
    repository,
    format,
    namespace,
    name,
    version,
    display,
    size_bytes: Math.round(size),
    downloads: total,
    bytes_served: Math.round(total * size),
    last_downloaded_at: last ? new Date(Math.max(created, last)).toISOString() : null,
    created_at: new Date(created).toISOString(),
    path,
    sha256: fakeSha256(`${repository}:${path}`),
    total,
  }
}

let cached: Entry[] | null = null

/** Every component of the mock world (built once). */
export function catalog(): Entry[] {
  if (cached) return cached
  const out: Entry[] = []
  for (const [repo, images] of Object.entries(DOCKER_IMAGES)) {
    images.forEach((img, i) => {
      for (const tag of img.tags.slice(0, 3)) out.push(entry(repo, 'docker', img.name, img.name, tag, `${img.name}:${tag}`, img.mib * MiB, `${img.name}/manifests/${tag}`, 900 / (i + 1)))
    })
  }
  NPM_PACKAGES.forEach((p, i) => {
    const scope = p.name.startsWith('@') ? p.name.split('/')[0]! : null
    out.push(entry('npm', 'npm', scope, p.name, p.latest, `${p.name}@${p.latest}`, p.kib * KiB, tarballPath(p.name, p.latest), 2400 / (i + 2)))
  })
  for (const repo of ['debian', 'ubuntu', 'debian-security']) {
    aptInstallPlan(repo).forEach((p, i) => {
      const [name, version] = p.name.split(' ')
      out.push(entry(repo, 'apt', null, name!, version ?? null, p.name, p.size, p.path, 320 / (i + 1)))
    })
  }
  for (const repo of ['rocky', 'almalinux']) {
    yumPackages(repo, 'x86_64').forEach((p, i) => {
      const cut = p.name.search(/-\d/)
      const name = cut > 0 ? p.name.slice(0, cut) : p.name
      out.push(entry(repo, 'yum', null, name, cut > 0 ? p.name.slice(cut + 1) : null, p.name, p.size, p.path, 260 / (i + 1)))
    })
  }
  for (const [repo, files] of Object.entries(RAW_FILES)) {
    files.forEach(([path, size], i) => {
      const parts = path.split('/')
      const file = parts.pop()!
      out.push(entry(repo, 'raw', parts.join('/'), file, null, file, size, path, 140 / (i + 1)))
    })
  }
  cached = out
  return out
}

function windowed(e: Entry, window: TopWindow): TopArtifact {
  const share = WINDOW_SHARE[window] * (0.7 + rng(hash(`${e.component_id}:${window}`))() * 0.6)
  const downloads = window === 'all' ? e.total : Math.min(e.total, Math.round(e.total * share))
  const { total: _total, ...rest } = e
  return { ...rest, downloads, bytes_served: downloads * e.size_bytes }
}

export function topArtifacts(visible: Repository[], by: TopBy, window: TopWindow, repo: string | null, limit: number): TopArtifacts {
  const names = new Set(visible.map((r) => r.name))
  const rows = catalog()
    .filter((e) => names.has(e.repository) && (!repo || e.repository === repo))
    .map((e) => (by === 'downloads' || by === 'bytes' ? windowed(e, window) : windowed(e, 'all')))
  const key: Record<TopBy, (a: TopArtifact) => number> = {
    downloads: (a) => a.downloads,
    bytes: (a) => a.bytes_served,
    size: (a) => a.size_bytes,
    recent: (a) => Date.parse(a.created_at),
  }
  const items = rows
    .filter((a) => by === 'size' || by === 'recent' || a.downloads > 0)
    .sort((a, b) => key[by](b) - key[by](a) || a.display.localeCompare(b.display))
    .slice(0, limit)
  return { by, window, items }
}

export function topRepositories(visible: Repository[], range: StatsRange, by: TopRepoBy, limit: number): TopRepositories {
  const scale = RANGES[range].seconds / 86400
  const items = visible
    .map((r) => ({
      repository: r.name,
      format: r.format,
      requests: Math.round(r.stats.requests_24h * scale),
      bytes_served: Math.round(r.stats.bytes_served_24h * scale),
      bytes_saved: Math.round((r.stats.bytes_saved_24h ?? 0) * scale),
      hit_ratio: r.stats.hit_ratio_24h,
      latency_p95_ms: r.stats.requests_24h ? [20, 50, 100, 200, 500][hash(r.name) % 5]! : null,
    }))
    .filter((r) => r.requests > 0)
  const key = { requests: (r: (typeof items)[number]) => r.requests, bytes: (r: (typeof items)[number]) => r.bytes_served, saved: (r: (typeof items)[number]) => r.bytes_saved }[by]
  return { range, by, items: items.sort((a, b) => key(b) - key(a)).slice(0, limit) }
}

export const HISTORY_STEPS: Record<HistoryRange, { seconds: number; step: number }> = {
  '7d': { seconds: 7 * 86400, step: 3600 },
  '30d': { seconds: 30 * 86400, step: 6 * 3600 },
  '90d': { seconds: 90 * 86400, step: 86400 },
  '1y': { seconds: 365 * 86400, step: 86400 },
}

/** Snapshots start 140 days ago (the "install date"), so the 1-year view has an empty start. */
const HISTORY_DAYS = 140

export function storageHistory(world: MockWorld, visible: Repository[], range: HistoryRange): StorageHistory {
  const { seconds, step } = HISTORY_STEPS[range]
  const end = Math.floor(Date.now() / 1000 / step) * step
  const n = Math.round(seconds / step)
  const stepsMs = Array.from({ length: n }, (_, i) => (end - (n - 1 - i) * step) * 1000)
  const visibleBytes = visible.reduce((a, r) => a + r.stats.size_bytes, 0)
  const visibleArtifacts = visible.reduce((a, r) => a + r.stats.artifact_count, 0)
  const used: (number | null)[] = []
  const stored: (number | null)[] = []
  const artifacts: (number | null)[] = []
  for (const t of stepsMs) {
    const age = (Date.now() - t) / DAY
    if (age > HISTORY_DAYS) {
      used.push(null)
      stored.push(null)
      artifacts.push(null)
      continue
    }
    // Growth: a fast fill in the first weeks, then steady; cleanup runs on Sundays dip it a little.
    const progress = 1 - age / HISTORY_DAYS
    const shape = 0.18 + 0.82 * Math.pow(progress, 0.75)
    const day = new Date(t).getDay()
    const dip = day === 1 ? 0.985 : 1
    const noise = 0.995 + rng(hash(`sh:${Math.floor(t / HOUR)}`))() * 0.01
    const f = Math.min(1, shape * dip * noise)
    used.push(Math.round(world.storageUsed * f))
    stored.push(Math.round(visibleBytes * f))
    artifacts.push(Math.round(visibleArtifacts * f))
  }
  return { range, step_s: step, timestamps: stepsMs.map((t) => new Date(t).toISOString()), used_bytes: used, stored_bytes: stored, artifacts }
}

/** `sha256:<hex>` or bare hex → hex; null when malformed. */
export function digestHex(raw: string): string | null {
  const hex = raw.toLowerCase().replace(/^sha256:/, '')
  return /^[0-9a-f]{64}$/.test(hex) ? hex : null
}

export function digestUsage(visible: Repository[], hex: string): DigestUsage {
  const names = new Set(visible.map((r) => r.name))
  const hit = catalog().find((e) => e.sha256 === hex)
  const items: DigestUse[] = []
  if (hit) {
    // The same file cached by every visible repository of the format that could hold it (shared blobs).
    for (const e of catalog()) {
      if (e.display !== hit.display || e.format !== hit.format || !names.has(e.repository)) continue
      items.push({ repository: e.repository, format: e.format, kind: 'asset', path: e.path, image: null, manifest_digest: null })
      if (e.format === 'docker') {
        items.push({ repository: e.repository, format: 'docker', kind: 'docker_layer', path: null, image: e.namespace, manifest_digest: `sha256:${fakeSha256(`${e.repository}:${e.display}:manifest`)}` })
      }
    }
  }
  return { digest: `sha256:${hex}`, size: hit ? hit.size_bytes : null, items, truncated: false }
}

/** Per-repository de-duplication savings and component counts for GET /stats/storage. */
export function repoDedup(r: Repository): { dedup_saved_bytes: number; components: number } {
  const share = r.format === 'docker' ? 0.11 : r.format === 'apt' || r.format === 'yum' ? 0.06 : 0.01
  const next = rng(hash(`dedup:${r.name}`))
  return { dedup_saved_bytes: Math.round(r.stats.size_bytes * share * (0.6 + next() * 0.8)), components: Math.round(r.stats.artifact_count * (r.format === 'docker' ? 0.08 : 0.9)) }
}
