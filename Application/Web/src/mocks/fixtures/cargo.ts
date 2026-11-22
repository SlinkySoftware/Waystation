// SPDX-License-Identifier: GPL-3.0-or-later
// Cargo crates for any Cargo repository created in mock mode: proxies hold real public crates.io crates (versions from
// the sparse index, the newest .crate files cached); hosted repositories hold an example internal crate with a
// pre-release and a yanked version.
import type { CargoCrate, CargoVersion } from '@/api/types'

import { fakeSha256, hash, KiB, rng } from '../rng'

const NOW = Date.now()
const DAY = 86_400_000

interface CrateSeed {
  name: string
  description: string
  versions: string[] // newest first (SemVer)
  yanked?: string[]
  features: string[]
  dependencies: number
  rust: string | null
  kib: number
  cached: number // newest versions whose .crate is cached (proxy)
}

const PUBLIC: CrateSeed[] = [
  { name: 'serde', description: 'A generic serialization/deserialization framework', versions: ['1.0.228', '1.0.227', '1.0.226', '1.0.225', '1.0.219', '1.0.210'], features: ['default', 'std', 'alloc', 'derive', 'rc', 'unstable'], dependencies: 2, rust: '1.56', kib: 82, cached: 3 },
  { name: 'serde_json', description: 'A JSON serialization file format', versions: ['1.0.145', '1.0.144', '1.0.143', '1.0.140'], features: ['default', 'std', 'alloc', 'preserve_order', 'float_roundtrip', 'arbitrary_precision', 'raw_value', 'unbounded_depth'], dependencies: 6, rust: '1.56', kib: 155, cached: 2 },
  { name: 'itoa', description: 'Fast integer primitive to string conversion', versions: ['1.0.15', '1.0.14', '1.0.13', '1.0.11'], features: ['no-panic'], dependencies: 1, rust: '1.36', kib: 11, cached: 2 },
  { name: 'ryu', description: 'Fast floating point to string conversion', versions: ['1.0.20', '1.0.19', '1.0.18'], features: ['no-panic', 'small'], dependencies: 1, rust: '1.36', kib: 47, cached: 1 },
  { name: 'libc', description: 'Raw FFI bindings to platform libraries like libc.', versions: ['0.2.177', '0.2.176', '0.2.175', '0.2.174', '0.2.172'], features: ['default', 'std', 'align', 'extra_traits', 'const-extern-fn', 'rustc-dep-of-std', 'use_std'], dependencies: 1, rust: '1.63', kib: 760, cached: 2 },
  { name: 'cfg-if', description: 'A macro to ergonomically define an item depending on a large number of #[cfg] parameters.', versions: ['1.0.4', '1.0.3', '1.0.1', '1.0.0', '0.1.10'], features: ['rustc-dep-of-std'], dependencies: 2, rust: '1.32', kib: 8, cached: 2 },
  { name: 'tokio', description: 'An event-driven, non-blocking I/O platform for writing asynchronous I/O backed applications.', versions: ['1.48.0', '1.47.1', '1.47.0', '1.46.1', '1.45.1', '1.38.2'], features: ['default', 'full', 'rt', 'rt-multi-thread', 'macros', 'net', 'io-util', 'fs', 'sync', 'time', 'signal', 'process', 'test-util', 'tracing'], dependencies: 12, rust: '1.70', kib: 800, cached: 1 },
]

const HOSTED: CrateSeed[] = [
  { name: 'example-internal', description: 'Shared types for internal services', versions: ['2.0.0-rc.1', '1.3.0', '1.2.1', '1.2.0', '1.1.0', '1.0.0'], yanked: ['1.2.0'], features: ['default', 'serde', 'tracing'], dependencies: 4, rust: '1.85', kib: 24, cached: 6 },
]

const isPrerelease = (version: string) => version.replace(/\+.*$/, '').includes('-')

function crateSize(seed: CrateSeed, version: string): number {
  const next = rng(hash(`crate:${seed.name}@${version}`))
  return Math.round(seed.kib * KiB * (0.85 + next() * 0.2))
}

function held(seed: CrateSeed, hosted: boolean): string[] {
  return hosted ? seed.versions : seed.versions.slice(0, seed.cached)
}

function summary(seed: CrateSeed, hosted: boolean): CargoCrate {
  const yanked = seed.yanked ?? []
  const release = seed.versions.find((v) => !yanked.includes(v) && !isPrerelease(v))
  const files = held(seed, hosted)
  return {
    name: seed.name,
    description: seed.description,
    latest: release ?? seed.versions[0] ?? null,
    version_count: seed.versions.length,
    cached_versions: files.length,
    size_bytes: files.reduce((sum, v) => sum + crateSize(seed, v), 0),
  }
}

const seeds = (hosted: boolean) => (hosted ? HOSTED : PUBLIC)

/** Crates of a proxy (public crates) or a hosted repository (internal crates), by name. */
export function cargoCrateList(hosted: boolean): CargoCrate[] {
  return [...seeds(hosted)].sort((a, b) => a.name.localeCompare(b.name)).map((s) => summary(s, hosted))
}

/** One crate (name in any case) and its versions, newest first; null when the repository has no such crate. */
export function cargoVersionsFor(repo: string, hosted: boolean, crate: string): { crate: CargoCrate; versions: CargoVersion[] } | null {
  const seed = seeds(hosted).find((s) => s.name.toLowerCase() === crate.trim().toLowerCase())
  if (!seed) return null
  const next = rng(hash(`cargover:${repo}:${seed.name}`))
  const files = held(seed, hosted)
  let when = NOW - next() * 10 * DAY
  const versions = seed.versions.map((version, i): CargoVersion => {
    when -= (3 + next() * 40) * DAY
    const cached = files.includes(version)
    const at = new Date(hosted ? when : NOW - next() * 30 * DAY).toISOString()
    return {
      version,
      prerelease: isPrerelease(version),
      yanked: (seed.yanked ?? []).includes(version),
      cksum: fakeSha256(`${seed.name}-${version}.crate`),
      rust_version: i < 4 ? seed.rust : null,
      dependencies: Math.max(0, seed.dependencies - (i > 3 ? 1 : 0)),
      features: i < 4 ? seed.features : seed.features.slice(0, Math.max(1, seed.features.length - 2)),
      cached,
      size: cached ? crateSize(seed, version) : null,
      published_by: hosted ? 'ci-builder' : null,
      published_at: hosted ? at : null,
      cached_at: !hosted && cached ? at : null,
      last_downloaded_at: cached ? new Date(NOW - next() * 4 * DAY).toISOString() : null,
      downloads: cached ? Math.round(next() * 600) : 0,
    }
  })
  return { crate: summary(seed, hosted), versions }
}
