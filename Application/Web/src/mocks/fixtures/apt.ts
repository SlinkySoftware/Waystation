// SPDX-License-Identifier: GPL-3.0-or-later
// apt suites and packages for the apt proxies (Debian trixie, Ubuntu noble, Raspberry Pi OS).
import type { AptPackage, AptSuite } from '@/api/types'

import { fakeSha256, hash, KiB, rng } from '../rng'

const NOW = Date.now()
const HOUR = 3_600_000

interface PkgSeed {
  name: string
  version: string
  section: string
  kib: number
  arch?: 'all'
}

const DEBIAN: PkgSeed[] = [
  { name: 'apt', version: '3.0.3', section: 'admin', kib: 1490 },
  { name: 'bash', version: '5.2.37-2+b5', section: 'shells', kib: 1520 },
  { name: 'ca-certificates', version: '20250419', section: 'misc', kib: 162, arch: 'all' },
  { name: 'coreutils', version: '9.7-3', section: 'utils', kib: 3010 },
  { name: 'curl', version: '8.14.1-2', section: 'web', kib: 268 },
  { name: 'dpkg', version: '1.22.21', section: 'admin', kib: 1610 },
  { name: 'git', version: '1:2.47.3-0+deb13u1', section: 'vcs', kib: 8890 },
  { name: 'gnupg', version: '2.4.7-21', section: 'utils', kib: 420, arch: 'all' },
  { name: 'htop', version: '3.4.1-5', section: 'utils', kib: 172 },
  { name: 'jq', version: '1.7.1-6+deb13u1', section: 'utils', kib: 74 },
  { name: 'libc6', version: '2.41-12', section: 'libs', kib: 2840 },
  { name: 'libcurl4t64', version: '8.14.1-2', section: 'libs', kib: 404 },
  { name: 'libssl3t64', version: '3.5.1-1', section: 'libs', kib: 2390 },
  { name: 'linux-image-6.12.57+deb13-amd64', version: '6.12.57-1', section: 'kernel', kib: 104_200 },
  { name: 'make', version: '4.4.1-2', section: 'devel', kib: 460 },
  { name: 'nginx', version: '1.26.3-3+deb13u1', section: 'httpd', kib: 610 },
  { name: 'openssh-server', version: '1:10.0p1-7', section: 'net', kib: 590 },
  { name: 'openssl', version: '3.5.1-1', section: 'utils', kib: 1480 },
  { name: 'postgresql-17', version: '17.6-0+deb13u1', section: 'database', kib: 17_850 },
  { name: 'python3.13', version: '3.13.5-2', section: 'python', kib: 720 },
  { name: 'rsync', version: '3.4.1+ds1-5', section: 'net', kib: 440 },
  { name: 'sudo', version: '1.9.16p2-3', section: 'admin', kib: 1960 },
  { name: 'systemd', version: '257.9-1~deb13u1', section: 'admin', kib: 3220 },
  { name: 'tmux', version: '3.5a-3', section: 'admin', kib: 470 },
  { name: 'unzip', version: '6.0-29', section: 'utils', kib: 166 },
  { name: 'vim', version: '2:9.1.1230-2', section: 'editors', kib: 1680 },
  { name: 'wget', version: '1.25.0-2', section: 'web', kib: 980 },
  { name: 'zstd', version: '1.5.7+dfsg-1', section: 'utils', kib: 96 },
  { name: 'docker.io', version: '26.1.5+dfsg1-9+b9', section: 'admin', kib: 28_300 },
  { name: 'gcc-14', version: '14.2.0-19', section: 'devel', kib: 19_400 },
]

const UBUNTU: PkgSeed[] = [
  { name: 'apt', version: '2.8.3', section: 'admin', kib: 1380 },
  { name: 'curl', version: '8.5.0-2ubuntu10.6', section: 'web', kib: 226 },
  { name: 'git', version: '1:2.43.0-1ubuntu7.3', section: 'vcs', kib: 3680 },
  { name: 'libc6', version: '2.39-0ubuntu8.6', section: 'libs', kib: 3150 },
  { name: 'libssl3t64', version: '3.0.13-0ubuntu3.6', section: 'libs', kib: 1940 },
  { name: 'linux-image-6.8.0-87-generic', version: '6.8.0-87.88', section: 'kernel', kib: 15_200 },
  { name: 'nginx', version: '1.24.0-2ubuntu7.5', section: 'httpd', kib: 520 },
  { name: 'openssh-server', version: '1:9.6p1-3ubuntu13.14', section: 'net', kib: 510 },
  { name: 'python3.12', version: '3.12.3-1ubuntu0.8', section: 'python', kib: 650 },
  { name: 'systemd', version: '255.4-1ubuntu8.11', section: 'admin', kib: 3480 },
  { name: 'vim', version: '2:9.1.0016-1ubuntu7.9', section: 'editors', kib: 1730 },
  { name: 'snapd', version: '2.68.5+ubuntu24.04.1', section: 'devel', kib: 31_400 },
]

const SUITES: Record<string, { suites: string[]; origin: string; components: string[]; archs: string[]; pkgs: PkgSeed[] }> = {
  debian: { suites: ['trixie', 'trixie-updates', 'trixie-backports'], origin: 'Debian', components: ['main', 'contrib', 'non-free', 'non-free-firmware'], archs: ['amd64', 'arm64', 'all'], pkgs: DEBIAN },
  'debian-security': { suites: ['trixie-security'], origin: 'Debian', components: ['main', 'contrib', 'non-free-firmware'], archs: ['amd64', 'arm64', 'all'], pkgs: DEBIAN.slice(0, 12) },
  ubuntu: { suites: ['noble', 'noble-updates', 'noble-security'], origin: 'Ubuntu', components: ['main', 'restricted', 'universe', 'multiverse'], archs: ['amd64', 'all'], pkgs: UBUNTU },
  raspberrypi: { suites: ['trixie'], origin: 'Raspberry Pi Foundation', components: ['main'], archs: ['arm64', 'armhf', 'all'], pkgs: DEBIAN.slice(0, 18) },
}

export function aptSuitesFor(repo: string): AptSuite[] {
  const cfg = SUITES[repo]
  if (!cfg) return []
  return cfg.suites.map((suite, i) => {
    const next = rng(hash(`suite:${repo}:${suite}`))
    const total = suite.includes('updates') || suite.includes('security') ? Math.round(300 + next() * 900) : suite.includes('backports') ? 6_812 : 64_419
    return {
      suite,
      origin: cfg.origin,
      codename: suite,
      components: cfg.components,
      architectures: cfg.archs,
      release_date: new Date(NOW - (i + 1) * (3 + next() * 20) * HOUR).toISOString(),
      last_refreshed_at: new Date(NOW - next() * 0.5 * HOUR).toISOString(),
      package_count: total,
      cached_package_count: Math.round(total * (0.004 + next() * 0.01)),
    }
  })
}

export function aptPackagesFor(repo: string, suite: string, arch?: string | null): AptPackage[] {
  const cfg = SUITES[repo]
  if (!cfg || !cfg.suites.includes(suite)) return []
  const archs = arch ? [arch] : cfg.archs.filter((a) => a !== 'all').slice(0, 1)
  const out: AptPackage[] = []
  for (const a of archs) {
    for (const p of cfg.pkgs) {
      const next = rng(hash(`pkg:${repo}:${suite}:${p.name}:${a}`))
      const pkgArch = p.arch ?? a
      const cached = next() < 0.62
      const pool = p.name.startsWith('lib') ? p.name.slice(0, 4) : p.name[0]!
      const ver = p.version.replace(/^\d+:/, '')
      out.push({
        name: p.name,
        version: suite.includes('backports') ? `${p.version}~bpo13+1` : p.version,
        architecture: pkgArch,
        component: 'main',
        section: p.section,
        size: Math.round(p.kib * KiB * (a === 'arm64' ? 0.94 : 1)),
        filename: `pool/main/${pool}/${p.name.replace(/-\d.*$/, '')}/${p.name}_${ver}_${pkgArch}.deb`,
        sha256: fakeSha256(`deb:${repo}:${p.name}:${p.version}:${pkgArch}`),
        cached,
        verified: cached ? next() > 0.05 : null,
        last_downloaded_at: cached ? new Date(NOW - next() * 72 * HOUR).toISOString() : null,
        downloads: cached ? Math.round(1 + next() * 160) : 0,
      })
    }
  }
  return out.sort((x, y) => x.name.localeCompare(y.name))
}

/** A run of packages for the generator's "apt install" scenario. */
export function aptInstallPlan(repo: string): { path: string; size: number; name: string }[] {
  const cfg = SUITES[repo]
  if (!cfg) return []
  const arch = cfg.archs[0]!
  return aptPackagesFor(repo, cfg.suites[0]!, arch).map((p) => ({ path: p.filename, size: p.size ?? 100 * KiB, name: `${p.name} ${p.version}` }))
}

export function aptSuiteNames(repo: string): string[] {
  return SUITES[repo]?.suites ?? []
}
