// SPDX-License-Identifier: GPL-3.0-or-later
// Swift packages for any Swift registry created in the mock: real public package names (GitHub owners as scopes),
// plausible versions, sizes and tools versions. A proxy lists the releases clients asked for (built from tags).
import type { SwiftPackage, SwiftRelease, SwiftScope } from '@/api/types'

import { hash, KiB } from '../rng'
import { BASE_URL } from './repositories'

const NOW = Date.now()
const DAY = 86_400_000

interface PackageSeed {
  scope: string
  name: string
  description: string
  tools: string
  /** Newest first (SemVer precedence). */
  versions: string[]
  kib: number
  alternates?: string[]
}

const PACKAGES: PackageSeed[] = [
  { scope: 'apple', name: 'swift-argument-parser', description: 'Straightforward, type-safe argument parsing for Swift', tools: '5.7', versions: ['1.6.1', '1.6.0', '1.5.1', '1.5.0'], kib: 412 },
  { scope: 'apple', name: 'swift-collections', description: 'Commonly used data structures for Swift', tools: '5.9', versions: ['1.2.1', '1.2.0', '1.1.4'], kib: 1290 },
  { scope: 'apple', name: 'swift-log', description: 'A Logging API for Swift', tools: '5.8', versions: ['1.6.4', '1.6.3', '1.6.2'], kib: 96 },
  { scope: 'apple', name: 'swift-nio', description: 'Event-driven network application framework for high performance protocol servers and clients, non-blocking.', tools: '5.10', versions: ['2.86.0', '2.85.0', '2.84.0'], kib: 2110, alternates: ['5.9'] },
  { scope: 'pointfreeco', name: 'swift-snapshot-testing', description: 'Delightful Swift snapshot testing.', tools: '5.9', versions: ['1.18.6', '1.18.5', '1.17.6'], kib: 388 },
  { scope: 'vapor', name: 'vapor', description: 'A server-side Swift HTTP web framework.', tools: '5.9', versions: ['4.117.0', '4.116.0', '4.115.1', '5.0.0-alpha.3'], kib: 1720 },
]

function summary(seed: PackageSeed): SwiftPackage {
  return {
    scope: seed.scope,
    name: seed.name,
    id: `${seed.scope}.${seed.name}`,
    latest_version: seed.versions.find((v) => !v.includes('-')) ?? null,
    release_count: seed.versions.length,
    removed_count: 0,
    description: seed.description,
    repository_url: `https://github.com/${seed.scope}/${seed.name}`,
  }
}

export function swiftScopeList(): SwiftScope[] {
  const counts = new Map<string, number>()
  for (const p of PACKAGES) counts.set(p.scope, (counts.get(p.scope) ?? 0) + 1)
  return [...counts].map(([scope, count]) => ({ scope, package_count: count })).sort((a, b) => a.scope.localeCompare(b.scope))
}

export function swiftPackageList(scope: string | null): SwiftPackage[] {
  return PACKAGES.filter((p) => !scope || p.scope === scope.toLowerCase())
    .map(summary)
    .sort((a, b) => a.id.localeCompare(b.id))
}

export function swiftReleasesFor(repo: string, hosted: boolean, scope: string, name: string): { package: SwiftPackage; releases: SwiftRelease[] } | null {
  const seed = PACKAGES.find((p) => p.scope === scope.toLowerCase() && p.name === name.toLowerCase())
  if (!seed) return null
  const releases = seed.versions.map((version, i): SwiftRelease => {
    const h = hash(`${repo}/${seed.scope}.${seed.name}/${version}`)
    const path = `${seed.scope}/${seed.name}/${version}.zip`
    const manifest = `${BASE_URL}/repository/${repo}/${seed.scope}/${seed.name}/${version}/Package.swift`
    return {
      version,
      prerelease: version.includes('-'),
      state: 'published',
      source: hosted ? 'hosted' : 'scm',
      published_at: new Date(NOW - (i * 29 + (h % 13)) * DAY).toISOString(),
      published_by: hosted ? 'ci-bot' : 'anonymous',
      removed_at: null,
      removed_by: null,
      checksum: h.toString(16).padStart(8, '0').repeat(8),
      size_bytes: Math.round(seed.kib * KiB * (0.9 + (h % 20) / 100)),
      tools_version: seed.tools,
      manifests: [
        { filename: 'Package.swift', swift_version: null, tools_version: seed.tools, size_bytes: 1200 + (h % 900), url: manifest },
        ...(seed.alternates ?? []).map((v) => ({ filename: `Package@swift-${v}.swift`, swift_version: v, tools_version: v, size_bytes: 1100 + (h % 700), url: `${manifest}?swift-version=${v}` })),
      ],
      signed: hosted && i === 0,
      signer: hosted && i === 0 ? 'CN=Release Signing,O=Example' : null,
      tag: hosted ? null : version,
      commit: hosted ? null : h.toString(16).padStart(8, '0').repeat(5),
      path,
      download_url: `${BASE_URL}/repository/${repo}/${path}`,
    }
  })
  return { package: summary(seed), releases }
}
