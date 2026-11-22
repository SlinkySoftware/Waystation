// SPDX-License-Identifier: GPL-3.0-or-later
// Conda packages for any conda repository created in mock mode: proxies list real public conda-forge packages (the
// newest files cached, one fetched before its listing was indexed); hosted repositories hold an example internal
// package uploaded by CI.
import type { CondaFile, CondaPackage } from '@/api/types'

import { fakeSha256, hash, KiB, rng } from '../rng'

const NOW = Date.now()
const DAY = 86_400_000

interface PackageSeed {
  name: string
  license: string
  summary: string | null
  subdirs: string[]
  versions: string[] // newest first (conda version order)
  builds: Record<string, string[]> // subdir -> build strings of each version
  depends: string[]
  kib: number
  cached: number // newest versions whose files are cached (proxy)
}

const PUBLIC: PackageSeed[] = [
  { name: 'ca-certificates', license: 'ISC', summary: null, subdirs: ['linux-64', 'osx-arm64'], versions: ['2025.10.5', '2025.8.3', '2025.4.26'], builds: { 'linux-64': ['hbd8a1cb_0'], 'osx-arm64': ['hbd8a1cb_0'] }, depends: ['__unix'], kib: 152, cached: 1 },
  { name: 'numpy', license: 'BSD-3-Clause', summary: null, subdirs: ['linux-64', 'osx-arm64'], versions: ['2.3.4', '2.3.3', '2.2.6', '2.1.3'], builds: { 'linux-64': ['py312h33ff503_0', 'py313hf6604e3_0'], 'osx-arm64': ['py312h6f6b8a5_0'] }, depends: ['libblas >=3.9.0,<4.0a0', 'libcblas >=3.9.0,<4.0a0', 'python_abi 3.12.* *_cp312'], kib: 8300, cached: 1 },
  { name: 'openssl', license: 'Apache-2.0', summary: null, subdirs: ['linux-64'], versions: ['3.6.0', '3.5.4', '3.5.3'], builds: { 'linux-64': ['h26f9b46_0'] }, depends: ['__glibc >=2.17,<3.0.a0', 'ca-certificates'], kib: 3100, cached: 1 },
  { name: 'python', license: 'Python-2.0', summary: null, subdirs: ['linux-64', 'osx-arm64'], versions: ['3.14.0', '3.13.9', '3.12.12'], builds: { 'linux-64': ['h32b2ec7_102_cp314'], 'osx-arm64': ['h40d2674_102_cp314'] }, depends: ['bzip2 >=1.0.8,<2.0a0', 'libffi >=3.5.2,<3.6.0a0', 'openssl >=3.5.4,<4.0a0'], kib: 31000, cached: 1 },
  { name: 'requests', license: 'Apache-2.0', summary: null, subdirs: ['noarch'], versions: ['2.32.5', '2.32.4', '2.32.3'], builds: { noarch: ['pyhd8ed1ab_0'] }, depends: ['certifi >=2017.4.17', 'idna >=2.5,<4', 'python >=3.9'], kib: 58, cached: 2 },
  { name: 'tzdata', license: 'LicenseRef-Public-Domain', summary: null, subdirs: ['noarch'], versions: ['2025b', '2025a', '2024b'], builds: { noarch: ['h78e105d_0'] }, depends: [], kib: 120, cached: 2 },
]

const HOSTED: PackageSeed[] = [
  { name: 'example-tools', license: 'Proprietary', summary: 'Internal command line tools', subdirs: ['noarch', 'linux-64'], versions: ['1.4.0', '1.3.2', '1.3.0rc1'], builds: { noarch: ['pyh4616a5c_0'], 'linux-64': ['h1234abc_0'] }, depends: ['python >=3.11', 'click >=8'], kib: 40, cached: 3 },
]

const PRERELEASE = /(rc|dev|a|b|alpha|beta)\d*$/i
const seeds = (hosted: boolean) => (hosted ? HOSTED : PUBLIC)

function fileCount(seed: PackageSeed): number {
  return seed.versions.length * seed.subdirs.reduce((sum, s) => sum + (seed.builds[s]?.length ?? 0), 0)
}

function summary(seed: PackageSeed): CondaPackage {
  return { name: seed.name, latest: seed.versions[0] ?? null, subdirs: seed.subdirs, file_count: fileCount(seed), summary: seed.summary, license: seed.license }
}

/** Packages of a proxy (conda-forge) or a hosted repository, by name; ``q`` is a name prefix. */
export function condaPackageList(hosted: boolean, q?: string): CondaPackage[] {
  const prefix = q?.trim().toLowerCase()
  return [...seeds(hosted)].filter((s) => !prefix || s.name.startsWith(prefix)).sort((a, b) => a.name.localeCompare(b.name)).map(summary)
}

/** One package and its files (newest version first), optionally of one subdir; null when unknown. */
export function condaFilesFor(repo: string, hosted: boolean, name: string, subdir?: string | null): { package: CondaPackage; files: CondaFile[] } | null {
  const seed = seeds(hosted).find((s) => s.name === name.trim().toLowerCase())
  if (!seed) return null
  const next = rng(hash(`conda:${repo}:${seed.name}`))
  const files: CondaFile[] = []
  seed.versions.forEach((version, i) => {
    const built = NOW - (i * 40 + next() * 20) * DAY
    for (const dir of seed.subdirs) {
      if (subdir && dir !== subdir) continue
      for (const build of seed.builds[dir] ?? []) {
        const filename = `${seed.name}-${version}-${build}.conda`
        const cached = hosted || i < seed.cached
        const unverified = !hosted && i === 0 && dir === 'noarch' && seed.name === 'tzdata'
        files.push({
          filename,
          subdir: dir,
          version,
          build,
          build_number: Number(build.match(/_(\d+)/)?.[1] ?? 0),
          prerelease: PRERELEASE.test(version),
          sha256: fakeSha256(`${dir}/${filename}`),
          md5: null,
          size: Math.round(seed.kib * KiB * (0.9 + next() * 0.2)),
          timestamp: new Date(built).toISOString(),
          verified: !unverified,
          depends: hosted ? seed.depends : [],
          license: hosted ? seed.license : null,
          cached,
          cached_at: cached ? new Date(NOW - next() * 20 * DAY).toISOString() : null,
          downloads: cached ? Math.round(next() * 400) : 0,
          last_downloaded_at: cached ? new Date(NOW - next() * 3 * DAY).toISOString() : null,
          uploaded_by: hosted ? 'ci-builder' : null,
        })
      }
    }
  })
  return { package: summary(seed), files }
}
