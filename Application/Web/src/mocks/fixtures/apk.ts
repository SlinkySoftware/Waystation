// SPDX-License-Identifier: GPL-3.0-or-later
// Alpine apk packages for any apk repository created in mock mode (the Nexus-style names are alpine-proxy and
// apk-hosted). Proxies hold real Alpine packages from the v3.22 main and community repositories of
// dl-cdn.alpinelinux.org with a few .apk files cached; hosted repositories hold the owner's own example package,
// all stored, with an uploader. Files are newest first in apk version order, one row per architecture, as the server
// sends them; checksums are APKINDEX `C:` values (Q1 + base64 SHA-1 of the control segment).
import type { ApkPackage, ApkVersion, Repository } from '@/api/types'

import { hash, KiB, rng } from '../rng'
import { BASE_URL } from './repositories'

const NOW = Date.now()
const DAY = 86_400_000
const BRANCH = 'v3.22'
const ARCHES = ['aarch64', 'x86_64']

interface ApkSeed {
  name: string
  description: string
  /** Newest first (apk version order). */
  versions: string[]
  repository: 'main' | 'community'
  license: string
  origin?: string
  url: string
  depends: string[]
  kib: number
}

const UPSTREAM: ApkSeed[] = [
  { name: 'busybox', description: 'Size optimized toolbox of many common UNIX utilities', versions: ['1.37.0-r19', '1.37.0-r18', '1.37.0-r12'], repository: 'main', license: 'GPL-2.0-only', url: 'https://pkgs.alpinelinux.org/package/v3.22/main/x86_64/busybox', depends: ['so:libc.musl-x86_64.so.1'], kib: 496 },
  { name: 'musl', description: 'the musl c library (libc) implementation', versions: ['1.2.5-r10', '1.2.5-r9'], repository: 'main', license: 'MIT', url: 'https://pkgs.alpinelinux.org/package/v3.22/main/x86_64/musl', depends: [], kib: 405 },
  { name: 'curl', description: 'URL retrieval utility and library', versions: ['8.14.1-r2', '8.14.1-r1', '8.12.1-r1'], repository: 'main', license: 'curl', url: 'https://curl.se/', depends: ['ca-certificates-bundle', 'so:libc.musl-x86_64.so.1', 'so:libcurl.so.4', 'so:libz.so.1'], kib: 262 },
  { name: 'libcurl', description: 'The multiprotocol file transfer library', versions: ['8.14.1-r2', '8.14.1-r1'], repository: 'main', origin: 'curl', license: 'curl', url: 'https://curl.se/', depends: ['so:libc.musl-x86_64.so.1', 'so:libcrypto.so.3', 'so:libssl.so.3', 'so:libz.so.1'], kib: 375 },
  { name: 'openssl', description: 'Toolkit for Transport Layer Security (TLS)', versions: ['3.5.4-r0', '3.5.1-r0', '3.5.0-r0'], repository: 'main', license: 'Apache-2.0', url: 'https://pkgs.alpinelinux.org/package/v3.22/main/x86_64/openssl', depends: ['so:libc.musl-x86_64.so.1', 'so:libcrypto.so.3', 'so:libssl.so.3'], kib: 351 },
  { name: 'ca-certificates', description: 'Common CA certificates PEM files from Mozilla', versions: ['20250911-r0', '20250619-r0'], repository: 'main', license: 'MPL-2.0 AND MIT', url: 'https://pkgs.alpinelinux.org/package/v3.22/main/x86_64/ca-certificates', depends: ['/bin/sh'], kib: 175 },
  { name: 'apk-tools', description: 'Alpine Package Keeper - package manager for alpine', versions: ['2.14.9-r3', '2.14.9-r2'], repository: 'main', license: 'GPL-2.0-only', url: 'https://gitlab.alpinelinux.org/alpine/apk-tools', depends: ['ca-certificates-bundle', 'so:libapk.so.2.14.0', 'so:libc.musl-x86_64.so.1'], kib: 33 },
  { name: 'bash', description: 'The GNU Bourne Again shell', versions: ['5.2.37-r0', '5.2.26-r0'], repository: 'main', license: 'GPL-3.0-or-later', url: 'https://www.gnu.org/software/bash/bash.html', depends: ['/bin/sh', 'so:libc.musl-x86_64.so.1', 'so:libreadline.so.8'], kib: 470 },
  { name: 'tzdata', description: 'Timezone data', versions: ['2025b-r0', '2025a-r0'], repository: 'main', license: 'Public-Domain', url: 'https://pkgs.alpinelinux.org/package/v3.22/main/x86_64/tzdata', depends: [], kib: 449 },
  { name: 'nginx', description: 'HTTP and reverse proxy server (stable version)', versions: ['1.28.0-r3', '1.28.0-r2', '1.26.3-r0'], repository: 'main', license: 'BSD-2-Clause', url: 'https://pkgs.alpinelinux.org/package/v3.22/main/x86_64/nginx', depends: ['/bin/sh', 'so:libc.musl-x86_64.so.1', 'so:libcrypto.so.3', 'so:libpcre2-8.so.0', 'so:libssl.so.3', 'so:libz.so.1'], kib: 636 },
  { name: 'docker', description: 'Pack, ship and run any application as a lightweight container', versions: ['28.3.3-r3', '28.3.3-r1', '28.1.1-r0'], repository: 'community', license: 'Apache-2.0', url: 'https://www.docker.io/', depends: ['containerd', 'docker-cli', 'docker-engine'], kib: 9 },
  { name: 'helm', description: 'The Kubernetes Package Manager', versions: ['3.18.6-r1', '3.18.4-r0'], repository: 'community', license: 'Apache-2.0', url: 'https://helm.sh/', depends: [], kib: 17_100 },
]

const HOSTED: ApkSeed[] = [
  { name: 'example-agent', description: 'Fleet telemetry agent for internal hosts', versions: ['2.4.1-r0', '2.4.0-r1', '2.3.7-r0'], repository: 'main', license: 'Proprietary', url: 'https://www.example.com/agent', depends: ['ca-certificates-bundle', 'so:libc.musl-x86_64.so.1'], kib: 4_200 },
]

function seedsFor(type: Repository['type']): ApkSeed[] {
  return type === 'hosted' ? HOSTED : UPSTREAM
}

function summary(seed: ApkSeed): ApkPackage {
  return { name: seed.name, latest_version: seed.versions[0] ?? null, arches: [...ARCHES], version_count: seed.versions.length, description: seed.description, origin: seed.origin ?? seed.name }
}

/** A plausible APKINDEX `C:` value: `Q1` + base64 of a 20-byte SHA-1 (28 characters). */
export function fakeApkChecksum(key: string): string {
  const next = rng(hash(`apk-c:${key}`))
  const bytes = String.fromCharCode(...Array.from({ length: 20 }, () => Math.floor(next() * 256)))
  return `Q1${btoa(bytes)}`
}

/** Packages of an apk repository, sorted by name, filtered by a case-insensitive substring of the name. */
export function apkPackageList(type: Repository['type'], q: string | null = null): ApkPackage[] {
  const needle = (q ?? '').trim().toLowerCase()
  return seedsFor(type)
    .filter((s) => !needle || s.name.toLowerCase().includes(needle))
    .map(summary)
    .sort((a, b) => a.name.localeCompare(b.name))
}

/** One package and its files (every architecture, newest version first); null when the repository does not hold it. */
export function apkVersionsFor(repo: string, type: Repository['type'], name: string): { package: ApkPackage; items: ApkVersion[] } | null {
  const seed = seedsFor(type).find((s) => s.name === name)
  if (!seed) return null
  const hosted = type === 'hosted'
  const next = rng(hash(`apkvers:${repo}:${name}`))
  let when = NOW - next() * 5 * DAY
  const items: ApkVersion[] = []
  seed.versions.forEach((version, i) => {
    when -= (4 + next() * 40) * DAY
    for (const arch of [...ARCHES].reverse()) {
      const directory = `${BRANCH}/${seed.repository}/${arch}`
      const path = `${directory}/${seed.name}-${version}.apk`
      const cached = hosted || (i === 0 && arch === 'x86_64') || next() < 0.15
      const size = Math.round(seed.kib * KiB * (arch === 'aarch64' ? 0.97 : 1) * (1 - i * 0.01))
      items.push({
        version,
        arch,
        branch: BRANCH,
        repository: seed.repository,
        directory,
        path,
        size_bytes: size,
        installed_size: Math.round(size * (2.1 + next())),
        checksum: fakeApkChecksum(`${repo}:${path}`),
        // Proxy files are verified against the cached APKINDEX; the oldest release was fetched before one was.
        verified: hosted || i < seed.versions.length - 1 || !cached,
        cached,
        license: seed.license,
        origin: seed.origin ?? seed.name,
        url: seed.url,
        build_time: new Date(when).toISOString(),
        depends: seed.depends.map((d) => (arch === 'aarch64' ? d.replace('x86_64', 'aarch64') : d)),
        uploaded_at: hosted ? new Date(when + 3_600_000).toISOString() : null,
        uploaded_by: hosted ? 'ci-builder' : null,
        download_url: `${BASE_URL}/repository/${repo}/${path}`,
      })
    }
  })
  return { package: summary(seed), items }
}
