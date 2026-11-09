// SPDX-License-Identifier: GPL-3.0-or-later
// Go modules for any Go proxy repository created in mock mode: real public module paths, tagged versions from the
// version list, a few pseudo-versions and +incompatible versions held only because a client asked for them.
import type { GoFile, GoModule, GoVersion } from '@/api/types'

import { fakeSha256, hash, KiB, rng } from '../rng'

const NOW = Date.now()
const DAY = 86_400_000

interface ModuleSeed {
  module: string
  tags: string[] // newest first
  extra?: string[] // cached but not in the version list (pseudo-versions, +incompatible)
  kib: number
  cached: number // newest tagged versions whose zip is cached
}

const MODULES: ModuleSeed[] = [
  { module: 'github.com/google/uuid', tags: ['v1.6.0', 'v1.5.0', 'v1.4.0', 'v1.3.1', 'v1.3.0', 'v1.2.0', 'v1.1.5', 'v1.1.4'], kib: 31, cached: 2 },
  { module: 'golang.org/x/text', tags: ['v0.30.0', 'v0.29.0', 'v0.28.0', 'v0.27.0', 'v0.26.0', 'v0.25.0'], extra: ['v0.0.0-20170915032832-14c0d48ead0c'], kib: 9_100, cached: 2 },
  { module: 'golang.org/x/sync', tags: ['v0.17.0', 'v0.16.0', 'v0.15.0', 'v0.14.0'], kib: 32, cached: 1 },
  { module: 'golang.org/x/sys', tags: ['v0.37.0', 'v0.36.0', 'v0.35.0'], extra: ['v0.0.0-20220715151400-c0bba94af5f8'], kib: 2_300, cached: 1 },
  { module: 'github.com/Masterminds/semver/v3', tags: ['v3.4.0', 'v3.3.1', 'v3.3.0', 'v3.2.1', 'v3.2.0'], kib: 34, cached: 2 },
  { module: 'github.com/pkg/errors', tags: ['v0.9.1', 'v0.9.0', 'v0.8.1', 'v0.8.0'], kib: 17, cached: 1 },
  { module: 'github.com/spf13/cobra', tags: ['v1.10.1', 'v1.10.0', 'v1.9.1', 'v1.9.0', 'v1.8.1'], kib: 180, cached: 1 },
  { module: 'github.com/prometheus/client_golang', tags: ['v1.23.2', 'v1.23.0', 'v1.22.0'], kib: 640, cached: 1 },
  { module: 'github.com/docker/docker', tags: ['v28.5.1+incompatible', 'v28.5.0+incompatible', 'v28.4.0+incompatible', 'v27.5.1+incompatible'], kib: 9_800, cached: 1 },
  { module: 'gopkg.in/yaml.v3', tags: ['v3.0.1', 'v3.0.0', 'v3.0.0-20210107192922-496545a6307b'], kib: 95, cached: 1 },
]

const PSEUDO = /^v\d+\.(?:0\.0-|\d+\.\d+-(?:[^+]*\.)?0\.)\d{14}-[A-Za-z0-9]+(?:\+[0-9A-Za-z.-]+)?$/

function zipSize(seed: ModuleSeed, version: string): number {
  const next = rng(hash(`gozip:${seed.module}@${version}`))
  return Math.round(seed.kib * KiB * (0.85 + next() * 0.2))
}

function versionsOf(seed: ModuleSeed): { version: string; listed: boolean; cached: boolean }[] {
  const listed = seed.tags.filter((v) => !PSEUDO.test(v))
  return [
    ...seed.tags.map((version, i) => ({ version, listed: listed.includes(version), cached: i < seed.cached || PSEUDO.test(version) })),
    ...(seed.extra ?? []).map((version) => ({ version, listed: false, cached: true })),
  ]
}

function summary(seed: ModuleSeed): GoModule {
  const held = versionsOf(seed).filter((v) => v.cached)
  return {
    module: seed.module,
    latest: seed.tags[0] ?? null,
    version_count: seed.tags.filter((v) => !PSEUDO.test(v)).length,
    cached_versions: held.length,
    size_bytes: held.reduce((sum, v) => sum + zipSize(seed, v.version), 0),
  }
}

export function goModuleList(): GoModule[] {
  return [...MODULES].sort((a, b) => a.module.localeCompare(b.module)).map(summary)
}

export function goVersionsFor(repo: string, module: string): { module: GoModule; versions: GoVersion[] } | null {
  const seed = MODULES.find((m) => m.module === module.trim())
  if (!seed) return null
  const next = rng(hash(`gover:${repo}:${seed.module}`))
  let when = NOW - next() * 20 * DAY
  const versions = versionsOf(seed).map(({ version, listed, cached }): GoVersion => {
    when -= (5 + next() * 60) * DAY
    const pseudo = PSEUDO.test(version)
    const cachedAt = new Date(NOW - next() * 30 * DAY).toISOString()
    const file = (kind: GoFile['kind'], size: number): GoFile => ({
      kind,
      size,
      sha256: fakeSha256(`${repo}/${seed.module}@${version}.${kind}`),
      cached_at: cachedAt,
      last_downloaded_at: new Date(NOW - next() * 4 * DAY).toISOString(),
      downloads: Math.round(next() * 400),
    })
    const files = cached ? [file('info', 60 + Math.round(next() * 40)), file('mod', 120 + Math.round(next() * 900)), file('zip', zipSize(seed, version))] : []
    return {
      version,
      time: cached ? new Date(when).toISOString() : null,
      prerelease: pseudo || /-/.test(version.replace(/\+incompatible$/, '')),
      pseudo,
      incompatible: version.endsWith('+incompatible'),
      listed,
      cached,
      files,
      size_bytes: files.reduce((sum, f) => sum + f.size, 0),
    }
  })
  return { module: summary(seed), versions }
}
