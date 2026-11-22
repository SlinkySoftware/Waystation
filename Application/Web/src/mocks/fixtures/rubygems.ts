// SPDX-License-Identifier: GPL-3.0-or-later
// RubyGems gems for any RubyGems repository created in mock mode (the Nexus-style names are rubygems-proxy,
// rubygems-hosted and rubygems-group). Proxies hold real rubygems.org gems with a few .gem files cached; hosted
// repositories hold the owner's own example gems, all stored, with a pusher; groups show both. Versions are newest
// first in RubyGems version order, one row per platform build, as the server sends them.
import type { Repository, RubygemsDependency, RubygemsGem, RubygemsVersion } from '@/api/types'

import { fakeSha256, hash, KiB, rng } from '../rng'
import { BASE_URL } from './repositories'

const NOW = Date.now()
const DAY = 86_400_000

interface GemSeed {
  name: string
  summary: string
  /** Newest first (RubyGems version order). */
  versions: string[]
  /** Platform builds besides `ruby` (native gems ship precompiled builds; the newest versions only). */
  platforms?: string[]
  ruby: string | null
  dependencies: RubygemsDependency[]
  kib: number
}

const NATIVE = ['x86_64-linux-gnu', 'aarch64-linux-gnu', 'arm64-darwin', 'x64-mingw-ucrt']

const UPSTREAM: GemSeed[] = [
  { name: 'rake', summary: 'Rake is a Make-like program implemented in Ruby', versions: ['13.3.0', '13.2.1', '13.2.0', '13.1.0', '13.0.6', '12.3.3'], ruby: '>= 2.3', dependencies: [], kib: 85 },
  { name: 'rack', summary: 'A modular Ruby webserver interface.', versions: ['3.2.3', '3.2.2', '3.1.18', '3.1.16', '3.0.18', '2.2.20'], ruby: '>= 2.4.0', dependencies: [], kib: 160 },
  { name: 'nokogiri', summary: 'Nokogiri (鋸) makes it easy and painless to work with XML and HTML from Ruby.', versions: ['1.18.10', '1.18.9', '1.18.8', '1.17.2', '1.16.8'], platforms: NATIVE, ruby: '>= 3.1.0', dependencies: [{ name: 'mini_portile2', requirement: '~> 2.8.2' }, { name: 'racc', requirement: '~> 1.4' }], kib: 4_400 },
  { name: 'rails', summary: 'Full-stack web application framework.', versions: ['8.1.0', '8.1.0.rc1', '8.0.3', '8.0.2', '7.2.2', '7.1.5'], ruby: '>= 3.2.0', dependencies: [{ name: 'actionpack', requirement: '= 8.1.0' }, { name: 'activerecord', requirement: '= 8.1.0' }, { name: 'activesupport', requirement: '= 8.1.0' }, { name: 'bundler', requirement: '>= 1.15.0' }, { name: 'railties', requirement: '= 8.1.0' }], kib: 7 },
  { name: 'puma', summary: 'A Ruby/Rack web server built for parallelism.', versions: ['7.1.0', '7.0.4', '6.6.1', '6.6.0', '6.5.0'], ruby: '>= 3.0', dependencies: [{ name: 'nio4r', requirement: '~> 2.0' }], kib: 280 },
  { name: 'rspec', summary: 'rspec-3.13.2', versions: ['3.13.2', '3.13.1', '3.13.0', '3.12.0', '3.11.0'], ruby: null, dependencies: [{ name: 'rspec-core', requirement: '~> 3.13.0' }, { name: 'rspec-expectations', requirement: '~> 3.13.0' }, { name: 'rspec-mocks', requirement: '~> 3.13.0' }], kib: 6 },
  { name: 'json', summary: 'JSON Implementation for Ruby', versions: ['2.15.1', '2.15.0', '2.13.2', '2.12.2', '2.10.2'], ruby: '>= 2.7', dependencies: [], kib: 95 },
  { name: 'sidekiq', summary: 'Simple, efficient background processing for Ruby', versions: ['8.0.8', '8.0.7', '7.3.9', '7.3.8', '6.5.12'], ruby: '>= 3.2.0', dependencies: [{ name: 'connection_pool', requirement: '>= 2.5.0' }, { name: 'json', requirement: '>= 2.9.0' }, { name: 'logger', requirement: '>= 1.6.2' }, { name: 'rack', requirement: '>= 3.1.0' }, { name: 'redis-client', requirement: '>= 0.23.2' }], kib: 260 },
  { name: 'bundler', summary: 'The best way to manage your application\'s dependencies', versions: ['2.7.2', '2.7.1', '2.6.9', '2.5.23', '2.4.22'], ruby: '>= 3.2.0', dependencies: [], kib: 420 },
]

const HOSTED: GemSeed[] = [
  { name: 'example-billing', summary: 'Invoicing and payment primitives shared by internal services', versions: ['2.1.0', '2.1.0.beta1', '2.0.3', '2.0.0', '1.4.2'], ruby: '>= 3.2', dependencies: [{ name: 'json', requirement: '~> 2.9' }, { name: 'money', requirement: '>= 6.19, < 7' }], kib: 48 },
  { name: 'example-auth-client', summary: 'Client for the internal identity service', versions: ['1.3.0', '1.2.1', '1.0.0.pre'], ruby: '>= 3.1', dependencies: [{ name: 'faraday', requirement: '~> 2.0' }], kib: 22 },
  { name: 'example-rubocop-config', summary: 'Shared RuboCop configuration', versions: ['0.9.0', '0.8.2', '0.8.1'], ruby: null, dependencies: [{ name: 'rubocop', requirement: '>= 1.60' }, { name: 'rubocop-rails', requirement: '>= 2.25' }], kib: 9 },
]

/** RubyGems treats any version with a letter in it as a prerelease. */
export function isPrerelease(version: string): boolean {
  return /[a-zA-Z]/.test(version)
}

function seedsFor(type: Repository['type']): GemSeed[] {
  return type === 'proxy' ? UPSTREAM : type === 'hosted' ? HOSTED : [...HOSTED, ...UPSTREAM]
}

function summary(seed: GemSeed, hosted: boolean): RubygemsGem {
  const next = rng(hash(`rubygems:${seed.name}`))
  return {
    name: seed.name,
    latest_version: seed.versions.find((v) => !isPrerelease(v)) ?? seed.versions[0] ?? null,
    version_count: hosted ? seed.versions.length : seed.versions.length + Math.floor(next() * 80),
    summary: seed.summary,
    updated_at: new Date(NOW - next() * 20 * DAY).toISOString(),
  }
}

/** Gems of a RubyGems repository, sorted by name, filtered by a case-insensitive substring. */
export function rubygemsGemList(type: Repository['type'], q: string | null = null): RubygemsGem[] {
  const needle = (q ?? '').trim().toLowerCase()
  return seedsFor(type)
    .filter((s) => !needle || s.name.toLowerCase().includes(needle))
    .map((s) => summary(s, HOSTED.includes(s)))
    .sort((a, b) => a.name.localeCompare(b.name))
}

/** One gem and its versions (one row per platform build), newest first; null when the repository does not hold it. */
export function rubygemsVersionsFor(repo: string, type: Repository['type'], name: string): { gem: RubygemsGem; items: RubygemsVersion[] } | null {
  // Gem names are case-sensitive, as on rubygems.org.
  const seed = seedsFor(type).find((s) => s.name === name)
  if (!seed) return null
  const hosted = HOSTED.includes(seed)
  const next = rng(hash(`rubygemsvers:${repo}:${name}`))
  let when = NOW - next() * 6 * DAY
  const items: RubygemsVersion[] = []
  seed.versions.forEach((version, i) => {
    when -= (3 + next() * 45) * DAY
    const platforms = ['ruby', ...(i < 3 ? (seed.platforms ?? []) : [])]
    for (const platform of platforms) {
      const cached = hosted || (i < 2 && (platform === 'ruby' || platform === 'x86_64-linux-gnu')) || next() < 0.2
      // As the server: gems/<name>-<version>[-<platform>].gem
      const path = `gems/${name}-${version}${platform === 'ruby' ? '' : `-${platform}`}.gem`
      const native = platform !== 'ruby'
      items.push({
        version,
        platform,
        prerelease: isPrerelease(version),
        cached,
        size_bytes: cached ? Math.round(seed.kib * KiB * (native ? 1.8 : 1) * (0.9 + next() * 0.2)) : null,
        sha256: cached ? fakeSha256(`${repo}:${path}`) : null,
        published_at: new Date(when).toISOString(),
        uploaded_by: hosted ? 'ci-builder' : null,
        ruby_requirement: seed.ruby,
        dependencies: seed.dependencies,
        path,
        download_url: `${BASE_URL}/repository/${repo}/${path}`,
      })
    }
  })
  return { gem: summary(seed, hosted), items }
}
