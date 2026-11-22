// SPDX-License-Identifier: GPL-3.0-or-later
// Composer packages for any Composer repository created in mock mode (the Nexus-style names are composer-proxy,
// composer-hosted and composer-group). Proxies hold real Packagist packages with a few archives cached; hosted
// repositories hold the owner's own example packages, all stored, with an uploader; groups show both. Versions are
// newest first in Composer version order, as the server sends them.
import type { ComposerPackage, ComposerVersion, Repository } from '@/api/types'

import { hash, KiB, rng } from '../rng'
import { BASE_URL } from './repositories'

const NOW = Date.now()
const DAY = 86_400_000

interface PackageSeed {
  name: string
  type: string
  description: string
  /** Newest first (Composer version order). */
  versions: string[]
  kib: number
}

const UPSTREAM: PackageSeed[] = [
  { name: 'monolog/monolog', type: 'library', description: 'Sends your logs to files, sockets, inboxes, databases and various web services', versions: ['3.9.0', '3.8.1', '3.8.0', '3.7.0', '2.10.0', '2.9.3', 'dev-main'], kib: 150 },
  { name: 'psr/container', type: 'library', description: 'Common Container Interface (PHP FIG PSR-11)', versions: ['2.0.2', '2.0.1', '2.0.0', '1.1.2', '1.1.1'], kib: 4 },
  { name: 'psr/log', type: 'library', description: 'Common interface for logging libraries', versions: ['3.0.2', '3.0.1', '3.0.0', '2.0.0', '1.1.4'], kib: 6 },
  { name: 'guzzlehttp/guzzle', type: 'library', description: 'Guzzle is a PHP HTTP client library', versions: ['7.10.0', '7.9.3', '7.9.2', '7.8.2', '6.5.8'], kib: 95 },
  { name: 'symfony/console', type: 'library', description: 'Eases the creation of beautiful and testable command line interfaces', versions: ['v7.3.4', 'v7.3.3', 'v7.3.0-RC1', 'v7.2.9', 'v6.4.26'], kib: 260 },
  { name: 'laravel/framework', type: 'library', description: 'The Laravel Framework.', versions: ['v12.32.5', 'v12.32.0', 'v12.31.1', 'v11.46.1', 'v10.48.29'], kib: 2_900 },
  { name: 'phpunit/phpunit', type: 'library', description: 'The PHP Unit Testing framework.', versions: ['12.4.0', '12.3.15', '11.5.42', '10.5.58'], kib: 600 },
  { name: 'doctrine/dbal', type: 'library', description: 'Powerful PHP database abstraction layer (DBAL) with many features for database schema introspection and management.', versions: ['4.3.4', '4.3.3', '4.2.5', '3.10.2', '4.4.0-beta1'], kib: 420 },
  { name: 'composer/installers', type: 'composer-plugin', description: 'A multi-framework Composer library installer', versions: ['v2.3.0', 'v2.2.0', 'v1.12.0'], kib: 40 },
]

const HOSTED: PackageSeed[] = [
  { name: 'acme/billing', type: 'library', description: 'Invoicing and payment primitives shared by internal services', versions: ['2.1.0', '2.1.0-beta.1', '2.0.3', '2.0.0', '1.4.2'], kib: 72 },
  { name: 'acme/auth-client', type: 'library', description: 'Client for the internal identity service', versions: ['1.3.0', '1.2.1', '1.0.0-alpha.2'], kib: 28 },
  { name: 'acme/theme', type: 'wordpress-theme', description: 'Corporate theme for the marketing sites', versions: ['3.0.0-RC1', '2.6.4', '2.6.3'], kib: 1_400 },
]

/** The stability Composer gives a version (the `dev-` prefix or the suffix after the numbers). */
export function stabilityOf(version: string): ComposerVersion['stability'] {
  const v = version.toLowerCase()
  if (v.startsWith('dev-') || v.endsWith('-dev')) return 'dev'
  if (/-?(rc)\.?\d*$/.test(v)) return 'RC'
  if (/-?(beta|b)\.?\d*$/.test(v)) return 'beta'
  if (/-?(alpha|a)\.?\d*$/.test(v)) return 'alpha'
  return 'stable'
}

/** Composer's normalised form: four numeric parts plus a stability suffix; branches as `dev-<branch>`. */
function normalize(version: string): string {
  if (version.startsWith('dev-')) return version
  const m = /^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:-?(.+))?$/.exec(version)
  if (!m) return version
  const nums = [m[1], m[2] ?? '0', m[3] ?? '0', m[4] ?? '0'].join('.')
  return m[5] ? `${nums}-${m[5].replace(/^RC/i, 'RC').replace(/\./g, '')}` : nums
}

function seedsFor(type: Repository['type']): PackageSeed[] {
  return type === 'proxy' ? UPSTREAM : type === 'hosted' ? HOSTED : [...HOSTED, ...UPSTREAM]
}

function summary(seed: PackageSeed, hosted: boolean): ComposerPackage {
  const next = rng(hash(`composer:${seed.name}`))
  return {
    name: seed.name,
    latest_version: seed.versions.find((v) => stabilityOf(v) === 'stable') ?? seed.versions[0] ?? null,
    version_count: hosted ? seed.versions.length : seed.versions.length + Math.floor(next() * 60),
    description: seed.description,
    type: seed.type,
    updated_at: new Date(NOW - next() * 20 * DAY).toISOString(),
  }
}

/** Packages of a Composer repository, sorted by name, filtered by a case-insensitive substring. */
export function composerPackageList(type: Repository['type'], q: string | null = null): ComposerPackage[] {
  const needle = (q ?? '').trim().toLowerCase()
  return seedsFor(type)
    .filter((s) => !needle || s.name.includes(needle))
    .map((s) => summary(s, HOSTED.includes(s)))
    .sort((a, b) => a.name.localeCompare(b.name))
}

/** One package and its versions, newest first; null when the repository does not hold it. */
export function composerVersionsFor(repo: string, type: Repository['type'], name: string): { package: ComposerPackage; items: ComposerVersion[] } | null {
  const lower = name.toLowerCase()
  const seed = seedsFor(type).find((s) => s.name === lower)
  if (!seed) return null
  const hosted = HOSTED.includes(seed)
  const next = rng(hash(`composervers:${repo}:${lower}`))
  let when = NOW - next() * 6 * DAY
  const items = seed.versions.map((version, i): ComposerVersion => {
    when -= (3 + next() * 45) * DAY
    const cached = hosted || i < 2 || next() < 0.3
    const reference = hosted ? null : Array.from({ length: 40 }, () => Math.floor(next() * 16).toString(16)).join('')
    // As the server: dists/<vendor>/<name>/<normalised version>/<source reference | name-version>.zip
    const normalized = normalize(version)
    const path = `dists/${lower}/${normalized}/${reference ?? `${lower.split('/')[1]}-${normalized}`}.zip`
    return {
      version,
      version_normalized: normalized,
      stability: stabilityOf(version),
      cached,
      size_bytes: cached ? Math.round(seed.kib * KiB * (0.9 + next() * 0.2)) : null,
      published_at: new Date(when).toISOString(),
      uploaded_by: hosted ? 'ci-builder' : null,
      reference,
      path: cached ? path : null,
      download_url: cached ? `${BASE_URL}/repository/${repo}/${path}` : null,
    }
  })
  return { package: summary(seed, hosted), items }
}
