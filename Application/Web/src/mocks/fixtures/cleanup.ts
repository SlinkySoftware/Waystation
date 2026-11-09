// SPDX-License-Identifier: GPL-3.0-or-later
// What cleanup policies can see in the mock: a deterministic inventory of cached versions per repository (the
// current versions from the browse fixtures plus older ones nobody pulls any more, docker untagged manifests), and
// the evaluator the preview and the simulated runs share - so, as on the server, a preview equals a run.
import type { CleanupCandidate, CleanupCriteria, CleanupRepositoryTotal, FieldError, Repository } from '@/api/types'

import { between, fakeSha256, hash, KiB, logBetween, MiB, rng } from '../rng'
import { aptPackagesFor, aptSuiteNames } from './apt'
import { DOCKER_IMAGES } from './docker'
import { NPM_PACKAGES, tarballPath } from './npm'
import { RAW_FILES } from './raw'

const DAY = 86_400_000
const PRERELEASE = /(alpha|beta|[-.~]rc\.?\d|canary|nightly|[-.]dev|[-.]next|^edge$|^dev$|~b\d)/i

interface Cascade {
  kind: 'manifest' | 'layer'
  version: string
  path: string
  size: number
}

export interface InventoryItem {
  key: string
  kind: 'asset' | 'tag' | 'manifest'
  name: string
  version: string
  path: string
  size: number
  created: number
  lastDownloaded: number | null
  prerelease: boolean
  /** Docker: manifests and layers that go with this tag or untagged manifest (nothing else uses them). */
  cascade: Cascade[]
}

/** "1.29.3" → "1.29.1" for k = 2, "1.29.3" → "1.28.5" for k = 8: an older version with the same shape. */
export function olderVersion(version: string, k: number): string {
  const values = [...version.matchAll(/\d+/g)].map((m) => Number(m[0]))
  if (!values.length) return `${version}.${k}`
  let i = values.length - 1
  values[i] = values[i]! - k
  while (values[i]! < 0 && i > 0) {
    values[i] = values[i]! + 10
    values[i - 1] = values[i - 1]! - 1
    i--
  }
  values[0] = Math.max(0, values[0]!)
  let n = 0
  return version.replace(/\d+/g, () => String(values[n++]))
}

function item(key: string, partial: Omit<InventoryItem, 'key' | 'prerelease' | 'cascade'> & { cascade?: Cascade[] }): InventoryItem {
  return { key, prerelease: PRERELEASE.test(partial.version), cascade: [], ...partial }
}

function layers(seed: string, image: string, count: number, mib: number, next: () => number): Cascade[] {
  return Array.from({ length: count }, (_, i) => {
    const digest = `sha256:${fakeSha256(`${seed}:layer:${i}`)}`
    return { kind: 'layer' as const, version: digest, path: `v2/${image}/blobs/${digest}`, size: Math.round(mib * MiB * between(0.04, 0.45, next)) }
  })
}

function dockerInventory(now: number, repo: string): InventoryItem[] {
  const out: InventoryItem[] = []
  for (const image of DOCKER_IMAGES[repo] ?? []) {
    const next = rng(hash(`cleanup:${repo}:${image.name}`))
    const manifest = (seed: string): Cascade => {
      const digest = `sha256:${fakeSha256(seed)}`
      return { kind: 'manifest', version: digest, path: `v2/${image.name}/manifests/${digest}`, size: Math.round(between(1.5, 9, next) * KiB) }
    }
    image.tags.forEach((tag, i) => {
      out.push(item(`${repo}:${image.name}:${tag}`, { kind: 'tag', name: image.name, version: tag, path: `v2/${image.name}/manifests/${tag}`, size: 0, created: now - (i * 4 + next() * 9) * DAY, lastDownloaded: now - next() * 4 * DAY }))
    })
    const versioned = image.tags.find((tag) => /\d/.test(tag)) ?? '1.0.0'
    const older = 3 + Math.floor(next() * 7)
    const seen = new Set(image.tags)
    for (let j = 0; j < older; j++) {
      const shape = j % 4
      const day = new Date(now - (j + 2) * 9 * DAY)
      const tag =
        shape === 0
          ? `nightly-${day.getUTCFullYear()}${String(day.getUTCMonth() + 1).padStart(2, '0')}${String(day.getUTCDate()).padStart(2, '0')}`
          : shape === 1
            ? `sha-${fakeSha256(`${repo}:${image.name}:${j}`).slice(0, 7)}`
            : shape === 2
              ? `${olderVersion(versioned, j + 1)}-rc.${1 + (j % 3)}`
              : olderVersion(versioned, j + 1)
      if (seen.has(tag)) continue
      seen.add(tag)
      const created = now - (18 + j * 26 + next() * 30) * DAY
      const seed = `${repo}:${image.name}:${tag}`
      out.push(
        item(seed, {
          kind: 'tag',
          name: image.name,
          version: tag,
          path: `v2/${image.name}/manifests/${tag}`,
          size: 0,
          created,
          lastDownloaded: next() < 0.3 ? null : created + next() * (now - created) * 0.35,
          cascade: [manifest(seed), ...layers(seed, image.name, 1 + Math.floor(next() * 3), image.mib, next)],
        }),
      )
    }
    const untagged = Math.floor(next() * 5)
    for (let u = 0; u < untagged; u++) {
      const seed = `${repo}:${image.name}:untagged:${u}`
      const m = manifest(seed)
      const created = now - (2 + next() * 140) * DAY
      out.push({ key: seed, kind: 'manifest', name: image.name, version: m.version, path: m.path, size: m.size, created, lastDownloaded: null, prerelease: false, cascade: layers(seed, image.name, Math.floor(next() * 3), image.mib, next) })
    }
  }
  return out
}

function npmInventory(now: number, repo: string): InventoryItem[] {
  const out: InventoryItem[] = []
  for (const seed of NPM_PACKAGES) {
    const next = rng(hash(`cleanup:${repo}:${seed.name}`))
    const count = 3 + Math.floor(next() * 8)
    let created = now - next() * 10 * DAY
    for (let k = 0; k < count; k++) {
      const version = k > 0 && next() < 0.18 ? `${olderVersion(seed.latest, k)}-beta.${k}` : olderVersion(seed.latest, k)
      const recent = k === 0 || next() < 0.35
      out.push(
        item(`${repo}:${seed.name}:${version}`, {
          kind: 'asset',
          name: seed.name,
          version,
          path: tarballPath(seed.name, version),
          size: Math.round(seed.kib * KiB * between(0.85, 1.1, next)),
          created,
          lastDownloaded: recent ? now - next() * 6 * DAY : next() < 0.4 ? null : created + next() * (now - created) * 0.5,
        }),
      )
      created -= (8 + next() * 45) * DAY
    }
  }
  return out
}

function aptInventory(now: number, repo: string): InventoryItem[] {
  const suite = aptSuiteNames(repo)[0]
  if (!suite) return []
  const out: InventoryItem[] = []
  for (const pkg of aptPackagesFor(repo, suite)) {
    const next = rng(hash(`cleanup:${repo}:${pkg.name}`))
    const count = 1 + Math.floor(next() * 4)
    let created = now - (1 + next() * 25) * DAY
    for (let k = 0; k < count; k++) {
      const version = k === 0 ? pkg.version : next() < 0.12 ? `${olderVersion(pkg.version, k)}~beta${k}` : olderVersion(pkg.version, k)
      out.push(
        item(`${repo}:${pkg.name}:${version}`, {
          kind: 'asset',
          name: pkg.name,
          version,
          path: pkg.filename.replace(pkg.version.replace(/^\d+:/, ''), version.replace(/^\d+:/, '')),
          size: Math.round((pkg.size ?? 100 * KiB) * between(0.92, 1.04, next)),
          created,
          lastDownloaded: k === 0 ? now - next() * 3 * DAY : next() < 0.5 ? null : created + next() * 20 * DAY,
        }),
      )
      created -= (30 + next() * 160) * DAY
    }
  }
  return out
}

const RPMS: [string, string, number][] = [
  ['kernel-core', '6.12.0-55.41.1.el10_0', 18_400],
  ['glibc', '2.39-37.el10', 2_100],
  ['openssl-libs', '3.2.2-16.el10', 2_300],
  ['systemd', '256-14.el10', 4_400],
  ['python3', '3.12.9-1.el10', 31],
  ['podman', '5.4.0-12.el10', 17_200],
  ['git-core', '2.47.3-1.el10', 5_100],
  ['curl', '8.12.1-1.el10', 320],
  ['vim-enhanced', '9.1.083-5.el10', 1_900],
  ['NetworkManager', '1.52.0-5.el10', 2_600],
]

function yumInventory(now: number, repo: string): InventoryItem[] {
  const out: InventoryItem[] = []
  for (const [name, latest, kib] of RPMS) {
    const next = rng(hash(`cleanup:${repo}:${name}`))
    let created = now - next() * 20 * DAY
    for (let k = 0; k < 1 + Math.floor(next() * 4); k++) {
      const version = olderVersion(latest.replace(/\.el.*$/, ''), k) + latest.slice(latest.indexOf('.el'))
      out.push(item(`${repo}:${name}:${version}`, { kind: 'asset', name, version, path: `Packages/${name}-${version}.x86_64.rpm`, size: Math.round(kib * KiB), created, lastDownloaded: k === 0 ? now - next() * 2 * DAY : next() < 0.5 ? null : created + 5 * DAY }))
      created -= (25 + next() * 90) * DAY
    }
  }
  return out
}

function rawInventory(now: number, repo: string): InventoryItem[] {
  return (RAW_FILES[repo] ?? []).map(([path, size]) => {
    const next = rng(hash(`cleanup:${repo}:${path}`))
    const parts = path.split('/')
    const version = parts[parts.indexOf('download') + 1] ?? 'unversioned'
    const created = now - logBetween(2, 400, next) * DAY
    return item(`${repo}:${path}`, { kind: 'asset', name: parts.slice(0, 2).join('/'), version, path, size: Math.round(size), created, lastDownloaded: next() < 0.3 ? null : created + next() * (now - created) })
  })
}

const cache = new Map<string, InventoryItem[]>()
const BUILDERS: Partial<Record<string, (now: number, repo: string) => InventoryItem[]>> = { docker: dockerInventory, npm: npmInventory, apt: aptInventory, yum: yumInventory, raw: rawInventory }

/** Everything cached in `repo` that a policy could select (stable within a page load). */
export function inventory(repo: Pick<Repository, 'name' | 'format'>, now: number = Date.now()): InventoryItem[] {
  const key = `${repo.format}:${repo.name}`
  let items = cache.get(key)
  if (!items) {
    const build = BUILDERS[repo.format]
    items = build ? build(now, repo.name) : []
    cache.set(key, items)
  }
  return items
}

const days = (ms: number) => Math.floor(ms / DAY)

function compile(pattern: string | null | undefined): RegExp | null {
  return pattern ? new RegExp(pattern) : null
}

/** Field errors for criteria (the server's rules): bounds, valid patterns, docker-only, at least one selector. */
export function validateCriteria(criteria: CleanupCriteria, format: string): FieldError[] {
  const errors: FieldError[] = []
  const bounds: [keyof CleanupCriteria, number][] = [
    ['last_downloaded_older_than_days', 36500],
    ['published_older_than_days', 36500],
    ['keep_latest_n_versions', 1000],
    ['untagged_older_than_days', 36500],
  ]
  for (const [key, max] of bounds) {
    const v = criteria[key]
    if (v != null && (typeof v !== 'number' || !Number.isInteger(v) || v < 1 || v > max)) errors.push({ field: `criteria.${key}`, message: `Enter a whole number from 1 to ${max}.`, code: 'range' })
  }
  for (const key of ['name_include', 'name_exclude', 'version_include', 'version_exclude'] as const) {
    try {
      compile(criteria[key])
    } catch {
      errors.push({ field: `criteria.${key}`, message: 'This is not a valid regular expression.', code: 'pattern' })
    }
  }
  if (criteria.untagged_older_than_days != null && format !== 'docker' && format !== '*') errors.push({ field: 'criteria.untagged_older_than_days', message: 'Untagged manifests exist only in docker repositories.', code: 'format' })
  const selecting = criteria.last_downloaded_older_than_days != null || criteria.published_older_than_days != null || criteria.keep_latest_n_versions != null || Boolean(criteria.name_include) || Boolean(criteria.version_include) || criteria.prerelease_only || criteria.untagged_older_than_days != null
  if (!selecting) errors.push({ field: 'criteria', message: 'Choose at least one criterion that selects content (age, keep latest, a pattern, pre-releases or untagged).', code: 'required' })
  return errors
}

export interface Evaluation {
  candidates: CleanupCandidate[]
  totals: CleanupRepositoryTotal[]
  /** Inventory keys a run would remove (so the next preview no longer lists them). */
  keys: string[]
}

/** Evaluate valid criteria against repositories: every criterion that is set must match (AND). */
export function evaluate(criteria: CleanupCriteria, repos: Repository[], removed: ReadonlySet<string> = new Set(), now: number = Date.now()): Evaluation {
  const nameIn = compile(criteria.name_include)
  const nameOut = compile(criteria.name_exclude)
  const verIn = compile(criteria.version_include)
  const verOut = compile(criteria.version_exclude)
  const tagSelecting = criteria.last_downloaded_older_than_days != null || criteria.published_older_than_days != null || criteria.keep_latest_n_versions != null || Boolean(nameIn) || Boolean(verIn) || criteria.prerelease_only
  const out: Evaluation = { candidates: [], totals: [], keys: [] }

  for (const repo of repos) {
    const items = inventory(repo, now).filter((i) => !removed.has(i.key))
    const total: CleanupRepositoryTotal = { name: repo.name, format: repo.format, count: 0, bytes: 0, truncated: false }
    const push = (it: InventoryItem, reason: string) => {
      const iso = (ms: number | null) => (ms == null ? null : new Date(ms).toISOString())
      const rows: CleanupCandidate[] = [
        { repository: repo.name, kind: it.kind, name: it.name, version: it.version, path: it.path, size: it.size, created_at: iso(it.created), last_downloaded_at: iso(it.lastDownloaded), reason },
        ...it.cascade.map((c): CleanupCandidate => ({ repository: repo.name, kind: c.kind, name: it.name, version: c.version, path: c.path, size: c.size, created_at: iso(it.created), last_downloaded_at: null, reason: `only used by ${it.kind === 'tag' ? `tag ${it.version}` : 'this manifest'}` })),
      ]
      out.candidates.push(...rows)
      out.keys.push(it.key)
      total.count += rows.length
      total.bytes += rows.reduce((a, r) => a + r.size, 0)
    }

    // Rank versions per component, newest first (docker - tags by recency).
    const ranks = new Map<string, Map<string, number>>()
    for (const it of items.filter((i) => i.kind !== 'manifest').sort((a, b) => b.created - a.created)) {
      const versions = ranks.get(it.name) ?? new Map<string, number>()
      if (!versions.has(it.version)) versions.set(it.version, versions.size)
      ranks.set(it.name, versions)
    }

    for (const it of [...items].sort((a, b) => a.created - b.created)) {
      if (nameIn && !nameIn.test(it.name)) continue
      if (nameOut?.test(it.name)) continue
      if (it.kind === 'manifest') {
        const age = now - it.created
        if (repo.format === 'docker' && criteria.untagged_older_than_days != null && age >= criteria.untagged_older_than_days * DAY) push(it, `untagged for ${days(age)} days`)
        continue
      }
      if (!tagSelecting) continue
      if (verIn && !verIn.test(it.version)) continue
      if (verOut?.test(it.version)) continue
      const reasons: string[] = []
      const unused = now - (it.lastDownloaded ?? it.created)
      if (criteria.last_downloaded_older_than_days != null) {
        if (unused < criteria.last_downloaded_older_than_days * DAY) continue
        reasons.push(it.lastDownloaded == null ? `never downloaded, cached ${days(unused)} days ago` : `not downloaded for ${days(unused)} days`)
      }
      if (criteria.published_older_than_days != null) {
        if (now - it.created < criteria.published_older_than_days * DAY) continue
        reasons.push(`${it.kind === 'tag' ? 'updated' : 'cached'} ${days(now - it.created)} days ago`)
      }
      if (criteria.keep_latest_n_versions != null) {
        const rank = ranks.get(it.name)?.get(it.version) ?? 0
        if (rank < criteria.keep_latest_n_versions) continue
        reasons.push(`newer ${it.kind === 'tag' ? 'tags' : 'versions'} kept (#${rank + 1})`)
      }
      if (criteria.prerelease_only) {
        if (!it.prerelease) continue
        reasons.push('pre-release')
      }
      if (reasons.length === 0) reasons.push(verIn ? `version matches /${criteria.version_include}/` : `name matches /${criteria.name_include}/`)
      push(it, reasons.join('; '))
    }
    out.totals.push(total)
  }
  return out
}
