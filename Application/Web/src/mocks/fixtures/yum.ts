// SPDX-License-Identifier: GPL-3.0-or-later
// yum/dnf repositories for the live generator: Rocky Linux 10, AlmaLinux 10, Oracle Linux 9 and EPEL 10, with
// their real path layouts under the upstream base (FORMATS.md "yum / dnf"). They feed `dnf makecache` / `dnf install`
// runs and the yum browse view (packages → files); any other yum repository browses the EL10 BaseOS/AppStream set.
import type { YumPackage, YumVersion } from '@/api/types'

import { fakeSha256, hash, KiB, MiB } from '../rng'
import { BASE_URL } from './repositories'

interface RpmSeed {
  name: string
  /** version-release */
  vr: string
  /** Repository id inside the distribution (BaseOS, AppStream, ...). */
  repo: string
  kib: number
  noarch?: boolean
}

interface YumDistro {
  /** Distribution name as dnf reports it in its User-Agent. */
  agent: string
  archs: string[]
  /** Repo ids enabled on a default install, with their primary.xml size (MiB). */
  repos: Record<string, number>
  base(repo: string, arch: string): string
  pkg(base: string, file: string): string
  pkgs: RpmSeed[]
}

const EL10_BASEOS: RpmSeed[] = [
  { name: 'bash', vr: '5.2.26-6.el10', repo: 'BaseOS', kib: 1_840 },
  { name: 'glibc', vr: '2.39-58.el10_1', repo: 'BaseOS', kib: 2_150 },
  { name: 'kernel-core', vr: '6.12.0-124.8.1.el10_1', repo: 'BaseOS', kib: 18_300 },
  { name: 'kernel-modules-core', vr: '6.12.0-124.8.1.el10_1', repo: 'BaseOS', kib: 37_100 },
  { name: 'openssl-libs', vr: '3.5.1-4.el10_1', repo: 'BaseOS', kib: 2_460 },
  { name: 'systemd', vr: '257-13.el10', repo: 'BaseOS', kib: 4_620 },
  { name: 'curl', vr: '8.12.1-2.el10', repo: 'BaseOS', kib: 318 },
  { name: 'dnf', vr: '4.20.0-18.el10', repo: 'BaseOS', kib: 512, noarch: true },
  { name: 'openssh-server', vr: '9.9p1-12.el10_1', repo: 'BaseOS', kib: 486 },
  { name: 'sudo', vr: '1.9.15-11.p5.el10', repo: 'BaseOS', kib: 1_090 },
  { name: 'NetworkManager', vr: '1.54.0-2.el10', repo: 'BaseOS', kib: 2_310 },
  { name: 'tar', vr: '1.35-7.el10', repo: 'BaseOS', kib: 870 },
  { name: 'rsync', vr: '3.4.1-2.el10', repo: 'BaseOS', kib: 424 },
  { name: 'ca-certificates', vr: '2025.2.80_v9.0.305-102.el10', repo: 'BaseOS', kib: 1_010, noarch: true },
  { name: 'tzdata', vr: '2025b-2.el10', repo: 'BaseOS', kib: 905, noarch: true },
]

const EL10_APPSTREAM: RpmSeed[] = [
  { name: 'git-core', vr: '2.47.3-1.el10', repo: 'AppStream', kib: 5_120 },
  { name: 'podman', vr: '5.6.0-6.el10_1', repo: 'AppStream', kib: 17_400 },
  { name: 'nginx-core', vr: '1.26.3-1.el10', repo: 'AppStream', kib: 688 },
  { name: 'python3-libs', vr: '3.12.11-2.el10', repo: 'AppStream', kib: 9_870 },
  { name: 'vim-enhanced', vr: '9.1.083-5.el10', repo: 'AppStream', kib: 2_030 },
  { name: 'postgresql-server', vr: '16.10-1.el10_1', repo: 'AppStream', kib: 6_640 },
  { name: 'container-selinux', vr: '2.240.0-1.el10', repo: 'AppStream', kib: 61, noarch: true },
  { name: 'tmux', vr: '3.4-5.el10', repo: 'AppStream', kib: 488 },
]

const OL9: RpmSeed[] = [
  { name: 'bash', vr: '5.1.8-9.el9', repo: 'baseos', kib: 1_720 },
  { name: 'glibc', vr: '2.34-168.0.1.el9_6', repo: 'baseos', kib: 2_010 },
  { name: 'openssl-libs', vr: '3.2.2-6.0.1.el9_5', repo: 'baseos', kib: 2_280 },
  { name: 'systemd', vr: '252-51.0.1.el9_6', repo: 'baseos', kib: 4_210 },
  { name: 'curl', vr: '7.76.1-31.el9_6', repo: 'baseos', kib: 296 },
  { name: 'openssh-server', vr: '8.7p1-45.0.1.el9', repo: 'baseos', kib: 465 },
  { name: 'sudo', vr: '1.9.5p2-10.el9_3', repo: 'baseos', kib: 1_010 },
  { name: 'NetworkManager', vr: '1.52.0-4.0.1.el9_6', repo: 'baseos', kib: 2_240 },
  { name: 'tar', vr: '1.34-7.el9', repo: 'baseos', kib: 880 },
  { name: 'kernel-uek-core', vr: '6.12.0-104.43.4.3.el9uek', repo: 'UEKR8', kib: 22_600 },
  { name: 'kernel-uek-modules', vr: '6.12.0-104.43.4.3.el9uek', repo: 'UEKR8', kib: 61_300 },
  { name: 'git-core', vr: '2.47.3-1.el9_6', repo: 'appstream', kib: 4_960 },
  { name: 'podman', vr: '5.4.0-13.0.1.el9_6', repo: 'appstream', kib: 16_200 },
  { name: 'nginx-core', vr: '1.20.1-22.0.1.el9_6', repo: 'appstream', kib: 590 },
  { name: 'python3.12-libs', vr: '3.12.9-1.el9_6', repo: 'appstream', kib: 9_610 },
]

const EPEL10: RpmSeed[] = [
  { name: 'htop', vr: '3.4.1-1.el10_1', repo: 'Everything', kib: 196 },
  { name: 'btop', vr: '1.4.5-1.el10_1', repo: 'Everything', kib: 1_020 },
  { name: 'fail2ban-server', vr: '1.1.0-9.el10_1', repo: 'Everything', kib: 612, noarch: true },
  { name: 'ncdu', vr: '2.8.2-1.el10_1', repo: 'Everything', kib: 264 },
  { name: 'restic', vr: '0.18.1-1.el10_1', repo: 'Everything', kib: 9_140 },
  { name: 'nload', vr: '0.7.4-34.el10_1', repo: 'Everything', kib: 74 },
]

const letterDir = (base: string, file: string) => `${base}Packages/${file[0]!.toLowerCase()}/${file}`

export const YUM_DISTROS: Record<string, YumDistro> = {
  rocky: {
    agent: 'Rocky Linux 10.1',
    archs: ['x86_64', 'aarch64'],
    repos: { BaseOS: 1.6, AppStream: 5.2 },
    base: (repo, arch) => `10/${repo}/${arch}/os/`,
    pkg: letterDir,
    pkgs: [...EL10_BASEOS, ...EL10_APPSTREAM],
  },
  almalinux: {
    agent: 'AlmaLinux 10.1',
    archs: ['x86_64', 'aarch64'],
    repos: { BaseOS: 1.5, AppStream: 5.0 },
    base: (repo, arch) => `10/${repo}/${arch}/os/`,
    pkg: (base, file) => `${base}Packages/${file}`,
    pkgs: [...EL10_BASEOS, ...EL10_APPSTREAM],
  },
  oracle: {
    agent: 'Oracle Linux Server 9.6',
    archs: ['x86_64', 'aarch64'],
    repos: { baseos: 2.4, appstream: 7.9, UEKR8: 0.6 },
    base: (repo, arch) => (repo === 'baseos' ? `repo/OracleLinux/OL9/baseos/latest/${arch}/` : `repo/OracleLinux/OL9/${repo}/${arch}/`),
    pkg: (base, file) => `${base}getPackage/${file}`,
    pkgs: OL9,
  },
  epel: {
    agent: 'Rocky Linux 10.1',
    archs: ['x86_64'],
    repos: { Everything: 4.6 },
    base: (_repo, arch) => `10/Everything/${arch}/`,
    pkg: letterDir,
    pkgs: EPEL10,
  },
}

export interface YumFile {
  path: string
  size: number
  name: string
  /** Metadata (repomd.xml) vs content validated against it (repodata, RPMs). */
  metadata: boolean
}

/** `dnf makecache`: repomd.xml for every enabled repo, then its primary / updateinfo / comps when they changed. */
export function yumMetadataFiles(distro: string, arch: string, changed: boolean): YumFile[] {
  const d = YUM_DISTROS[distro]
  if (!d) return []
  const out: YumFile[] = []
  for (const [repo, primaryMiB] of Object.entries(d.repos)) {
    const base = d.base(repo, arch)
    out.push({ path: `${base}repodata/repomd.xml`, size: Math.round((3.8 + primaryMiB * 0.3) * KiB), name: `${repo} repomd.xml`, metadata: true })
    if (!changed) continue
    const salt = `${distro}:${repo}:${arch}:${new Date().toISOString().slice(0, 13)}`
    out.push({ path: `${base}repodata/${fakeSha256(`${salt}:primary`)}-primary.xml.zst`, size: Math.round(primaryMiB * MiB), name: `${repo} primary.xml`, metadata: false })
    if (repo !== 'UEKR8') out.push({ path: `${base}repodata/${fakeSha256(`${salt}:updateinfo`)}-updateinfo.xml.zst`, size: Math.round(primaryMiB * 0.42 * MiB), name: `${repo} updateinfo.xml`, metadata: false })
    if (repo === 'BaseOS' || repo === 'AppStream' || repo === 'baseos') out.push({ path: `${base}repodata/${fakeSha256(`${salt}:comps`)}-comps-${repo}.${arch}.xml.zst`, size: Math.round(primaryMiB * 0.05 * MiB), name: `${repo} comps.xml`, metadata: false })
  }
  return out
}

/** RPMs a `dnf install` / `dnf upgrade` could pull from this distribution. */
export function yumPackages(distro: string, arch: string): YumFile[] {
  const d = YUM_DISTROS[distro]
  if (!d) return []
  return d.pkgs.map((p) => {
    const a = p.noarch ? 'noarch' : arch
    const file = `${p.name}-${p.vr}.${a}.rpm`
    return { path: d.pkg(d.base(p.repo, arch), file), size: Math.round(p.kib * KiB * (arch === 'aarch64' ? 0.96 : 1)), name: `${p.name}-${p.vr}`, metadata: false }
  })
}

/** dnf's User-Agent: `libdnf (<distro>; <variant>; Linux.<arch>)`. */
export function dnfAgent(distro: string, arch: string): string {
  const d = YUM_DISTROS[distro]
  return `libdnf (${d?.agent ?? 'Rocky Linux 10.1'}; ${distro === 'oracle' ? 'server' : 'generic'}; Linux.${arch})`
}

const ROCKY = 'rocky'

/** The rpm `version-release` of a seed one or two updates back (`-6.el10` → `-5.el10`). */
function older(vr: string, steps: number): string {
  return vr.replace(/-(\d+)/, (_m, rel: string) => `-${Math.max(1, Number(rel) - steps)}`)
}

function seedsOf(repo: string): { d: YumDistro; key: string } {
  const key = YUM_DISTROS[repo] ? repo : ROCKY
  return { d: YUM_DISTROS[key]!, key }
}

/** Packages the browse view lists: the distribution's seeds, newest first, with their architectures. */
export function yumPackageList(repo: string): YumPackage[] {
  const { d } = seedsOf(repo)
  const byName = new Map<string, YumPackage>()
  for (const p of d.pkgs) {
    if (byName.has(p.name)) continue
    byName.set(p.name, { name: p.name, latest_version: p.vr, arches: p.noarch ? ['noarch'] : [...d.archs].sort(), version_count: 1 + (hash(p.name) % 3), summary: null })
  }
  return [...byName.values()].sort((a, b) => a.name.localeCompare(b.name))
}

/** One package's files (every architecture, newest version first), the newest cached on the first architecture. */
export function yumVersionsFor(repo: string, name: string): { package: YumPackage; versions: YumVersion[] } | null {
  const { d } = seedsOf(repo)
  const seed = d.pkgs.find((p) => p.name === name)
  const pkg = yumPackageList(repo).find((p) => p.name === name)
  if (!seed || !pkg) return null
  const versions: YumVersion[] = []
  for (let step = 0; step < pkg.version_count; step++) {
    const vr = step === 0 ? seed.vr : older(seed.vr, step)
    const [version, release] = [vr.slice(0, vr.indexOf('-')), vr.slice(vr.indexOf('-') + 1)]
    for (const arch of pkg.arches) {
      const base = d.base(seed.repo, arch === 'noarch' ? d.archs[0]! : arch)
      const path = d.pkg(base, `${seed.name}-${vr}.${arch}.rpm`)
      versions.push({
        evr: vr,
        epoch: '0',
        version,
        release,
        arch,
        path,
        directory: base.replace(/\/$/, ''),
        size_bytes: Math.round(seed.kib * KiB * (arch === 'aarch64' ? 0.96 : 1)),
        checksum: `sha256:${fakeSha256(path)}`,
        cached: step === 0 && arch === pkg.arches[0],
        uploaded_at: null,
        download_url: `${BASE_URL}/repository/${repo}/${path}`,
      })
    }
  }
  return { package: pkg, versions }
}
