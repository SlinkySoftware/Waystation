// SPDX-License-Identifier: GPL-3.0-or-later
// NuGet packages for any NuGet repository created in mock mode (the Nexus defaults are nuget.org-proxy, nuget-hosted
// and nuget-group). Proxies hold real nuget.org packages with a few versions cached; hosted repositories hold the
// owner's own example packages, all stored, with an uploader; groups show both. Unlists are remembered per repository
// for the session. Versions are newest first in NuGet version order, as the server sends them.
import type { NugetPackage, NugetVersion, Repository } from '@/api/types'

import { hash, KiB, rng } from '../rng'
import { BASE_URL } from './repositories'

const NOW = Date.now()
const DAY = 86_400_000

interface PackageSeed {
  id: string
  authors: string
  description: string
  /** Newest first (NuGet version order). */
  versions: string[]
  kib: number
}

const UPSTREAM: PackageSeed[] = [
  { id: 'Newtonsoft.Json', authors: 'James Newton-King', description: 'A popular high-performance JSON framework for .NET', versions: ['13.0.4', '13.0.3', '13.0.2', '13.0.1', '12.0.3', '12.0.2', '12.0.1', '11.0.2'], kib: 2_300 },
  { id: 'Serilog', authors: 'Serilog Contributors', description: 'Simple .NET logging with fully-structured events', versions: ['4.3.1-dev-02395', '4.3.0', '4.2.0', '4.1.0', '4.0.2', '4.0.1', '3.1.1'], kib: 740 },
  { id: 'Serilog.Sinks.Console', authors: 'Serilog Contributors', description: 'A Serilog sink that writes log events to the console/terminal.', versions: ['6.1.1', '6.0.0', '5.0.1', '5.0.0', '4.1.0'], kib: 120 },
  { id: 'Humanizer.Core', authors: 'Claire Novotny, Mehdi Khalili', description: 'Humanizer core package that contains the library and the neutral language (English) resources', versions: ['3.0.0-rc.30', '2.14.1', '2.13.14', '2.12.4', '2.11.10'], kib: 370 },
  { id: 'Polly', authors: 'Michael Wolfenden, App vNext', description: 'Polly is a .NET resilience and transient-fault-handling library that allows developers to express resilience and transient fault handling policies.', versions: ['8.6.4', '8.6.3', '8.5.2', '8.4.2', '8.3.1', '7.2.4'], kib: 410 },
  { id: 'Microsoft.Extensions.Logging.Abstractions', authors: 'Microsoft', description: 'Logging abstractions for Microsoft.Extensions.Logging.', versions: ['10.0.0', '10.0.0-rc.2.25502.107', '9.0.11', '9.0.10', '8.0.3'], kib: 1_100 },
  { id: 'xunit', authors: 'jamesnewkirk, bradwilson', description: 'A developer testing framework, built to support Test Driven Development, with a design goal of extreme simplicity and alignment with framework features.', versions: ['2.9.3', '2.9.2', '2.9.0', '2.8.1', '2.7.1'], kib: 6 },
  { id: 'FluentValidation', authors: 'Jeremy Skinner', description: 'A validation library for .NET that uses a fluent interface to construct strongly-typed validation rules.', versions: ['12.1.0', '12.0.0', '11.11.0', '11.10.0'], kib: 520 },
  { id: 'Dapper', authors: 'Sam Saffron, Marc Gravell, Nick Craver', description: 'A high performance Micro-ORM supporting SQL Server, MySQL, Sqlite, SqlCE, Firebird etc.', versions: ['2.1.66', '2.1.35', '2.1.28', '2.1.24', '2.0.151'], kib: 590 },
]

const HOSTED: PackageSeed[] = [
  { id: 'Example.Common', authors: 'Example Ltd', description: 'Shared primitives and extension methods for internal services', versions: ['2.4.0', '2.4.0-beta.2', '2.3.1', '2.3.0', '2.2.0'], kib: 84 },
  { id: 'Example.Telemetry', authors: 'Example Ltd', description: 'OpenTelemetry defaults and Serilog enrichers for internal services', versions: ['1.2.0', '1.1.3', '1.1.2', '1.0.0'], kib: 62 },
  { id: 'Example.Build.Tasks', authors: 'Example Ltd', description: 'MSBuild tasks for versioning and packaging', versions: ['0.9.0-alpha.4', '0.8.2', '0.8.1'], kib: 140 },
]

/** `${repo}/${id-lower}/${version}`: versions unlisted in this session. */
const unlisted = new Set<string>()

const isPrerelease = (version: string): boolean => version.includes('-')

function seedsFor(type: Repository['type']): PackageSeed[] {
  return type === 'proxy' ? UPSTREAM : type === 'hosted' ? HOSTED : [...HOSTED, ...UPSTREAM]
}

function summary(seed: PackageSeed, hosted: boolean): NugetPackage {
  const next = rng(hash(`nupkg:${seed.id}`))
  return {
    name: seed.id.toLowerCase(),
    id: seed.id,
    latest_version: seed.versions.find((v) => !isPrerelease(v)) ?? seed.versions[0] ?? null,
    version_count: hosted ? seed.versions.length : seed.versions.length + Math.floor(next() * 40),
    description: seed.description,
    authors: seed.authors,
    updated_at: new Date(NOW - next() * 20 * DAY).toISOString(),
  }
}

/** Packages of a NuGet repository, sorted by id (case-insensitive), filtered by a case-insensitive substring. */
export function nugetPackageList(type: Repository['type'], q: string | null = null): NugetPackage[] {
  const needle = (q ?? '').trim().toLowerCase()
  return seedsFor(type)
    .filter((s) => !needle || s.id.toLowerCase().includes(needle))
    .map((s) => summary(s, HOSTED.includes(s)))
    .sort((a, b) => a.name.localeCompare(b.name))
}

/** One package and its versions, newest first; null when the repository does not hold it. */
export function nugetVersionsFor(repo: string, type: Repository['type'], packageId: string): { package: NugetPackage; items: NugetVersion[] } | null {
  const lower = packageId.toLowerCase()
  const seed = seedsFor(type).find((s) => s.id.toLowerCase() === lower)
  if (!seed) return null
  const hosted = HOSTED.includes(seed)
  const next = rng(hash(`nuvers:${repo}:${lower}`))
  let when = NOW - next() * 6 * DAY
  const items = seed.versions.map((version, i): NugetVersion => {
    when -= (4 + next() * 40) * DAY
    const cached = hosted || i < 2 || next() < 0.25
    const path = `v3-flatcontainer/${lower}/${version.toLowerCase()}/${lower}.${version.toLowerCase()}.nupkg`
    return {
      version,
      prerelease: isPrerelease(version),
      listed: !unlisted.has(`${repo}/${lower}/${version}`),
      cached,
      size_bytes: cached ? Math.round(seed.kib * KiB * (0.9 + next() * 0.2)) : null,
      published_at: new Date(when).toISOString(),
      uploaded_by: hosted ? 'ci-builder' : null,
      path,
      download_url: `${BASE_URL}/repository/${repo}/${path}`,
    }
  })
  return { package: summary(seed, hosted), items }
}

/** Unlist or relist a version of a hosted repository; false when the package or version is unknown. */
export function nugetSetListing(repo: string, packageId: string, version: string, listed: boolean): boolean {
  const lower = packageId.toLowerCase()
  const seed = HOSTED.find((s) => s.id.toLowerCase() === lower)
  if (!seed || !seed.versions.includes(version)) return false
  const key = `${repo}/${lower}/${version}`
  if (listed) unlisted.delete(key)
  else unlisted.add(key)
  return true
}
