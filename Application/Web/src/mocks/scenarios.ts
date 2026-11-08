// SPDX-License-Identifier: GPL-3.0-or-later
// What real clients do, as lists of transfer specs: a docker pull, an npm install, an apt update + install,
// a dnf makecache + install, a raw download. The generator (world.ts) plays these out with realistic speeds and
// concurrency.
import type { CacheState, Format } from '@/api/types'

import { aptInstallPlan, aptSuiteNames } from './fixtures/apt'
import { DOCKER_IMAGES, pullPlan } from './fixtures/docker'
import { NPM_PACKAGES, tarballPath } from './fixtures/npm'
import { RAW_FILES } from './fixtures/raw'
import { dnfAgent, YUM_DISTROS, yumMetadataFiles, yumPackages } from './fixtures/yum'
import { between, fakeSha256, KiB, logBetween, MiB, pick } from './rng'

export interface Client {
  ip: string
  user: string | null
  agent: string
}

export interface Spec {
  repository: string
  format: Format
  artifact: string
  path: string
  component: string | null
  size: number
  cache: CacheState
  /** Key shared by identical assets so concurrent fetches coalesce. */
  key: string
  /** Fail part-way with this message. */
  error?: string
  /** Extra time to first byte (slow upstream), ms. */
  slowMs?: number
}

export interface JobPlan {
  label: string
  client: Client
  concurrency: number
  specs: Spec[]
}

const DOCKER_AGENTS = ['docker/28.5.1 go/go1.24.8 git-commit/f8215cc kernel/6.12.48 os/linux arch/amd64', 'containerd/2.1.4', 'docker/27.5.1 go/go1.22.11 os/linux arch/arm64', 'buildkit/v0.25.1']
const NPM_AGENTS = ['npm/11.6.2 node/v24.11.0 linux x64 workspaces/false', 'pnpm/10.20.0 npm/? node/v24.11.0 linux x64', 'npm/10.9.4 node/v22.21.1 linux x64 ci/gitea-actions', 'yarn/1.22.22 npm/? node/v22.21.1 linux x64']
const APT_AGENTS = ['Debian APT-HTTP/1.3 (3.0.3) non-interactive', 'Debian APT-HTTP/1.3 (3.0.3)', 'Debian APT-HTTP/1.3 (3.1.11ubuntu1) non-interactive']
const CURL_AGENT = 'curl/8.14.1'

const USERS = [null, null, null, 'ci-runner', 'build-bot', 'alice', 'bob']

export function randomClient(agents: string[]): Client {
  const net = pick(['192.0.2', '198.51.100', '203.0.113'])
  return { ip: `${net}.${Math.floor(between(10, 250))}`, user: pick(USERS), agent: pick(agents) }
}

/** `docker pull`: index → platform manifest → config → layers (3 at a time, like dockerd). */
export function dockerPull(cold: boolean, forceRepo?: string): JobPlan {
  const repo = forceRepo ?? (Math.random() < 0.68 ? 'dockerhub-proxy' : 'ghcr-proxy')
  const seed = pick(DOCKER_IMAGES[repo]!)
  const tag = pick(seed.tags)
  const short = seed.name.replace(/^library\//, '')
  const component = repo === 'ghcr-proxy' ? `ghcr.io/${seed.name}:${tag}` : `${short}:${tag}`
  const metaState: CacheState = cold ? 'FETCHING' : Math.random() < 0.6 ? 'REVALIDATED' : 'HIT'
  const base = { repository: repo, format: 'docker' as Format, component }
  const specs: Spec[] = [
    { ...base, artifact: `${seed.name}:${tag}`, path: `v2/${seed.name}/manifests/${tag}`, size: Math.round(between(1.5, 10) * KiB), cache: metaState, key: `${repo}:${seed.name}:idx:${tag}` },
    { ...base, artifact: `${seed.name}@sha256:${fakeSha256(component + 'cfg').slice(0, 12)} (config)`, path: `v2/${seed.name}/blobs/sha256:${fakeSha256(component + 'cfg')}`, size: Math.round(between(1.4, 9) * KiB), cache: cold ? 'FETCHING' : 'HIT', key: `${repo}:${component}:cfg` },
  ]
  const layers = pullPlan(repo, seed.name, tag)
  for (const layer of layers) {
    const shared = layer === layers[0] && Math.random() < 0.5
    const fail = cold && Math.random() < 0.025
    specs.push({
      ...base,
      artifact: `${seed.name}@${layer.digest.slice(0, 19)}`,
      path: `v2/${seed.name}/blobs/${layer.digest}`,
      size: layer.size,
      cache: cold && !shared ? 'FETCHING' : 'HIT',
      key: `${repo}:${layer.digest}`,
      error: fail ? pick(['upstream connection reset by peer', 'digest mismatch: upstream sent sha256:' + fakeSha256(String(Math.random())).slice(0, 12) + '…, not cached', 'upstream read timeout after 60 s']) : undefined,
    })
  }
  // The manifest fetch failing ends the pull: the client never learns the layers.
  if (cold && repo === 'dockerhub-proxy' && Math.random() < 0.04) specs.splice(0, specs.length, { ...specs[0]!, cache: 'ERROR', error: '429 Too Many Requests from registry-1.docker.io (pull rate limit)' })
  return { label: `docker pull ${component}`, client: randomClient(DOCKER_AGENTS), concurrency: 3, specs }
}

/** `npm ci`: packuments (revalidated) then a burst of tarballs, ~15 sockets. */
export function npmInstall(): JobPlan {
  const count = Math.floor(between(18, 60))
  const specs: Spec[] = []
  const chosen = Array.from({ length: count }, () => pick(NPM_PACKAGES))
  const uniq = [...new Set(chosen)]
  for (const p of uniq.slice(0, 8)) {
    specs.push({ repository: 'npm', format: 'npm', artifact: `${p.name} (packument)`, path: p.name, component: null, size: Math.round(logBetween(6, 900) * KiB), cache: Math.random() < 0.8 ? 'REVALIDATED' : 'HIT', key: `npm:meta:${p.name}` })
  }
  for (const p of chosen) {
    const fetch = Math.random() < 0.12
    const version = Math.random() < 0.7 ? p.latest : p.latest.replace(/\d+$/, (m) => String(Math.max(0, Number(m) - 1 - Math.floor(Math.random() * 3))))
    specs.push({
      repository: 'npm',
      format: 'npm',
      artifact: tarballPath(p.name, version).split('/-/')[1]!,
      path: tarballPath(p.name, version),
      component: `${p.name}@${version}`,
      size: Math.round(p.kib * KiB * between(0.9, 1.1)),
      cache: fetch ? 'FETCHING' : 'HIT',
      key: `npm:${p.name}@${version}`,
      error: fetch && Math.random() < 0.03 ? 'integrity check failed (sha512 mismatch); not cached' : undefined,
    })
  }
  return { label: 'npm ci', client: randomClient(NPM_AGENTS), concurrency: 15, specs }
}

const APT_INDEX_MIB: Record<string, number> = { debian: 9.6, ubuntu: 2.1, 'debian-security': 0.3, 'ubuntu-security': 0.4, raspberrypi: 0.2 }

/** `apt update && apt install`: InRelease per suite, by-hash indexes, then .debs. */
export function aptRun(): JobPlan {
  const repo = pick(['debian', 'debian', 'debian', 'debian-security', 'debian-security', 'ubuntu', 'ubuntu', 'ubuntu-security', 'raspberrypi', 'raspberrypi'])
  const suites = aptSuiteNames(repo)
  const arch = repo === 'raspberrypi' ? (Math.random() < 0.7 ? 'arm64' : 'armhf') : Math.random() < 0.85 ? 'amd64' : 'arm64'
  const fresh = Math.random() < 0.3
  const base = { repository: repo, format: 'apt' as Format, component: null }
  const specs: Spec[] = []
  for (const suite of suites) {
    const main = suite === suites[0]
    specs.push({ ...base, artifact: `dists/${suite}/InRelease`, path: `dists/${suite}/InRelease`, size: Math.round(between(main ? 48 : 30, main ? 160 : 70) * KiB), cache: fresh && main ? 'FETCHING' : 'REVALIDATED', key: `${repo}:${suite}:inrelease` })
    if (!main && !fresh) continue
    const indexMiB = (APT_INDEX_MIB[repo] ?? 0.3) * (main ? 1 : 0.04)
    specs.push({ ...base, artifact: `dists/${suite}/main/binary-${arch}/Packages.xz`, path: `dists/${suite}/main/binary-${arch}/by-hash/SHA256/${fakeSha256(suite + arch + String(fresh))}`, size: Math.round(indexMiB * MiB), cache: fresh ? 'FETCHING' : 'HIT', key: `${repo}:${suite}:${arch}:packages:${fresh}` })
    if (main) specs.push({ ...base, artifact: `dists/${suite}/main/i18n/Translation-en.xz`, path: `dists/${suite}/main/i18n/by-hash/SHA256/${fakeSha256(suite + 'tr')}`, size: Math.round(indexMiB * 0.14 * MiB), cache: 'HIT', key: `${repo}:${suite}:tr` })
  }
  const plan = aptInstallPlan(repo, suites[0], arch)
  const n = Math.floor(between(3, 14))
  for (let i = 0; i < n && plan.length; i++) {
    const p = pick(plan)
    const fetch = Math.random() < (repo.endsWith('-security') ? 0.4 : 0.25)
    specs.push({ ...base, artifact: p.path.split('/').pop()!, path: p.path, component: p.name, size: p.size, cache: fetch ? 'FETCHING' : 'HIT', key: `${repo}:${p.path}` })
  }
  return { label: `apt install (${repo})`, client: randomClient(APT_AGENTS), concurrency: 3, specs }
}

/**
 * `dnf makecache && dnf install`: repomd.xml per enabled repo, changed repodata, then RPMs. `oracle` is degraded
 * (slow, intermittent 5xx, stale metadata); `epel` is auto-blocked (stale metadata, cached RPMs only).
 */
export function dnfRun(forceRepo?: string): JobPlan {
  const repo = forceRepo ?? pick(['rocky', 'rocky', 'rocky', 'almalinux', 'almalinux', 'oracle', 'oracle', 'epel'])
  const distro = YUM_DISTROS[repo]!
  const arch = Math.random() < 0.8 ? distro.archs[0]! : (distro.archs[1] ?? distro.archs[0]!)
  const degraded = repo === 'oracle'
  const blocked = repo === 'epel'
  const changed = !blocked && Math.random() < 0.3
  const base = { repository: repo, format: 'yum' as Format, component: null }
  const specs: Spec[] = []
  for (const f of yumMetadataFiles(repo, arch, changed)) {
    let cache: CacheState = f.metadata ? (changed ? 'FETCHING' : 'REVALIDATED') : changed ? 'FETCHING' : 'HIT'
    let error: string | undefined
    if (blocked && f.metadata) cache = 'STALE'
    else if (degraded && f.metadata && Math.random() < 0.35) cache = 'STALE'
    else if (degraded && !f.metadata && cache === 'FETCHING' && Math.random() < 0.3) {
      cache = 'ERROR'
      error = pick(['503 Service Unavailable from yum.oracle.com', '502 Bad Gateway from yum.oracle.com'])
    }
    specs.push({ ...base, artifact: f.path.split('/').pop()!, path: f.path, size: f.size, cache, key: `${repo}:${f.path}`, error, slowMs: degraded ? between(600, 2600) : undefined })
  }
  const pkgs = yumPackages(repo, arch)
  const n = Math.floor(between(2, blocked ? 4 : 12))
  for (let i = 0; i < n && pkgs.length; i++) {
    const p = pick(pkgs)
    const fetch = Math.random() < (blocked ? 0.5 : 0.22)
    let cache: CacheState = fetch ? 'FETCHING' : 'HIT'
    let error: string | undefined
    if (fetch && blocked) {
      cache = 'ERROR'
      error = 'upstream auto-blocked (dl.fedoraproject.org unreachable); not cached'
    } else if (fetch && degraded && Math.random() < 0.2) {
      error = pick(['upstream read timeout after 60 s', '503 Service Unavailable from yum.oracle.com'])
    }
    specs.push({ ...base, artifact: p.path.split('/').pop()!, path: p.path, component: p.name, size: p.size, cache, key: `${repo}:${p.path}`, error, slowMs: degraded && fetch ? between(800, 3000) : undefined })
  }
  return { label: `dnf install (${repo})`, client: randomClient([dnfAgent(repo, arch)]), concurrency: 3, specs }
}

/** A curl download of a release binary. */
export function rawDownload(): JobPlan {
  const repo = 'raw-releases'
  const files = RAW_FILES[repo]!.filter(([, size]) => size > 100 * KiB)
  const [path, size] = pick(files)
  const fetch = Math.random() < 0.2
  return {
    label: `curl ${path}`,
    client: randomClient([CURL_AGENT, 'Wget/1.25.0', 'Ansible-get_url/2.19']),
    concurrency: 1,
    specs: [{ repository: repo, format: 'raw', artifact: path.split('/').pop()!, path, component: null, size: Math.round(size), cache: fetch ? 'FETCHING' : 'HIT', key: `${repo}:${path}` }],
  }
}

/** Steady background: a single small metadata hit (manifest HEAD, packument, InRelease, repomd.xml). */
export function trickle(): JobPlan {
  const kind = Math.random()
  if (kind < 0.35) {
    const repo = Math.random() < 0.75 ? 'dockerhub-proxy' : 'ghcr-proxy'
    const seed = pick(DOCKER_IMAGES[repo]!)
    const tag = pick(seed.tags)
    const component = repo === 'ghcr-proxy' ? `ghcr.io/${seed.name}:${tag}` : `${seed.name.replace(/^library\//, '')}:${tag}`
    return { label: 'manifest HEAD', client: randomClient(DOCKER_AGENTS), concurrency: 1, specs: [{ repository: repo, format: 'docker', artifact: `${seed.name}:${tag}`, path: `v2/${seed.name}/manifests/${tag}`, component, size: Math.round(between(1.5, 8) * KiB), cache: Math.random() < 0.7 ? 'HIT' : 'REVALIDATED', key: `trickle:${Math.random()}` }] }
  }
  if (kind < 0.5) {
    const repo = pick(['debian', 'debian-security', 'ubuntu', 'ubuntu-security', 'raspberrypi'])
    const suite = pick(aptSuiteNames(repo))
    return { label: 'InRelease', client: randomClient(APT_AGENTS), concurrency: 1, specs: [{ repository: repo, format: 'apt', artifact: `dists/${suite}/InRelease`, path: `dists/${suite}/InRelease`, component: null, size: Math.round(between(30, 160) * KiB), cache: Math.random() < 0.6 ? 'HIT' : 'REVALIDATED', key: `trickle:${Math.random()}` }] }
  }
  if (kind < 0.62) {
    const repo = pick(['rocky', 'rocky', 'almalinux', 'oracle'])
    const f = yumMetadataFiles(repo, 'x86_64', false)[0]!
    return { label: 'repomd.xml', client: randomClient([dnfAgent(repo, 'x86_64')]), concurrency: 1, specs: [{ repository: repo, format: 'yum', artifact: 'repomd.xml', path: f.path, component: null, size: f.size, cache: repo === 'oracle' ? 'STALE' : Math.random() < 0.6 ? 'HIT' : 'REVALIDATED', key: `trickle:${Math.random()}` }] }
  }
  const p = pick(NPM_PACKAGES)
  return { label: 'packument', client: randomClient(NPM_AGENTS), concurrency: 1, specs: [{ repository: 'npm', format: 'npm', artifact: `${p.name} (packument)`, path: p.name, component: null, size: Math.round(logBetween(6, 700) * KiB), cache: Math.random() < 0.6 ? 'HIT' : 'REVALIDATED', key: `trickle:${Math.random()}` }] }
}
