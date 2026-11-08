// SPDX-License-Identifier: GPL-3.0-or-later
// What real clients do, as lists of transfer specs: a docker pull, an npm install, an apt update + install,
// a raw download. The generator (world.ts) plays these out with realistic speeds and concurrency.
import type { CacheState, Format } from '@/api/types'

import { aptInstallPlan } from './fixtures/apt'
import { DOCKER_IMAGES, pullPlan } from './fixtures/docker'
import { NPM_PACKAGES, tarballPath } from './fixtures/npm'
import { RAW_FILES } from './fixtures/raw'
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
}

export interface JobPlan {
  label: string
  client: Client
  concurrency: number
  specs: Spec[]
}

const DOCKER_AGENTS = ['docker/28.5.1 go/go1.24.8 git-commit/f8215cc kernel/6.12.48 os/linux arch/amd64', 'containerd/2.1.4', 'docker/27.5.1 go/go1.22.11 os/linux arch/arm64', 'buildkit/v0.25.1']
const NPM_AGENTS = ['npm/11.6.2 node/v24.11.0 linux x64 workspaces/false', 'pnpm/10.20.0 npm/? node/v24.11.0 linux x64', 'npm/10.9.4 node/v22.21.1 linux x64 ci/gitea-actions', 'yarn/1.22.22 npm/? node/v22.21.1 linux x64']
const APT_AGENT = 'Debian APT-HTTP/1.3 (3.0.3) non-interactive'
const CURL_AGENT = 'curl/8.14.1'

const USERS = [null, null, null, 'ci-runner', 'build-bot', 'alice', 'bob']

export function randomClient(agents: string[]): Client {
  const net = pick(['192.0.2', '198.51.100', '203.0.113'])
  return { ip: `${net}.${Math.floor(between(10, 250))}`, user: pick(USERS), agent: pick(agents) }
}

/** `docker pull`: index → platform manifest → config → layers (3 at a time, like dockerd). */
export function dockerPull(cold: boolean, forceRepo?: string): JobPlan {
  const repo = forceRepo ?? (Math.random() < 0.7 ? 'docker-hub' : Math.random() < 0.65 ? 'ghcr' : 'quay')
  const seed = pick(DOCKER_IMAGES[repo]!)
  const tag = pick(seed.tags)
  const short = seed.name.replace(/^library\//, '')
  const component = `${short}:${tag}`
  const flaky = repo === 'quay'
  const metaState: CacheState = flaky && Math.random() < 0.5 ? 'STALE' : cold ? 'FETCHING' : Math.random() < 0.6 ? 'REVALIDATED' : 'HIT'
  const base = { repository: repo, format: 'docker' as Format, component }
  const specs: Spec[] = [
    { ...base, artifact: `${seed.name}:${tag}`, path: `v2/${seed.name}/manifests/${tag}`, size: Math.round(between(1.5, 10) * KiB), cache: metaState, key: `${repo}:${seed.name}:idx:${tag}` },
    { ...base, artifact: `${seed.name}@sha256:${fakeSha256(component + 'cfg').slice(0, 12)} (config)`, path: `v2/${seed.name}/blobs/sha256:${fakeSha256(component + 'cfg')}`, size: Math.round(between(1.4, 9) * KiB), cache: cold ? 'FETCHING' : 'HIT', key: `${repo}:${component}:cfg` },
  ]
  for (const layer of pullPlan(repo, seed.name, tag)) {
    const shared = layer === pullPlan(repo, seed.name, tag)[0] && Math.random() < 0.5
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
  if (flaky && Math.random() < 0.3) specs[0] = { ...specs[0]!, cache: 'ERROR', error: '503 Service Unavailable from quay.io' }
  return { label: `docker pull ${component}`, client: randomClient(DOCKER_AGENTS), concurrency: 3, specs }
}

/** `npm ci`: packuments (revalidated) then a burst of tarballs, ~15 sockets. */
export function npmInstall(): JobPlan {
  const count = Math.floor(between(18, 60))
  const specs: Spec[] = []
  const chosen = Array.from({ length: count }, () => pick(NPM_PACKAGES))
  const uniq = [...new Set(chosen)]
  for (const p of uniq.slice(0, 8)) {
    specs.push({ repository: 'npm-proxy', format: 'npm', artifact: `${p.name} (packument)`, path: p.name, component: null, size: Math.round(logBetween(6, 900) * KiB), cache: Math.random() < 0.8 ? 'REVALIDATED' : 'HIT', key: `npm:meta:${p.name}` })
  }
  for (const p of chosen) {
    const fetch = Math.random() < 0.12
    const version = Math.random() < 0.7 ? p.latest : p.latest.replace(/\d+$/, (m) => String(Math.max(0, Number(m) - 1 - Math.floor(Math.random() * 3))))
    specs.push({
      repository: 'npm-proxy',
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

/** `apt update && apt install`: InRelease, by-hash indexes, then .debs. */
export function aptRun(): JobPlan {
  const repo = pick(['debian', 'debian', 'debian', 'ubuntu', 'ubuntu', 'debian-security', 'raspberrypi'])
  const suite = repo === 'ubuntu' ? 'noble' : repo === 'debian-security' ? 'trixie-security' : 'trixie'
  const arch = repo === 'raspberrypi' ? 'arm64' : 'amd64'
  const fresh = Math.random() < 0.3
  const base = { repository: repo, format: 'apt' as Format, component: null }
  const specs: Spec[] = [
    { ...base, artifact: `dists/${suite}/InRelease`, path: `dists/${suite}/InRelease`, size: Math.round(between(48, 160) * KiB), cache: fresh ? 'FETCHING' : 'REVALIDATED', key: `${repo}:${suite}:inrelease` },
    { ...base, artifact: `dists/${suite}/main/binary-${arch}/Packages.xz`, path: `dists/${suite}/main/binary-${arch}/by-hash/SHA256/${fakeSha256(suite + arch + String(fresh))}`, size: Math.round((repo === 'debian' ? 9.6 : repo === 'ubuntu' ? 1.4 : 0.3) * MiB), cache: fresh ? 'FETCHING' : 'HIT', key: `${repo}:${suite}:${arch}:packages:${fresh}` },
    { ...base, artifact: `dists/${suite}/main/i18n/Translation-en.xz`, path: `dists/${suite}/main/i18n/by-hash/SHA256/${fakeSha256(suite + 'tr')}`, size: Math.round((repo === 'debian' ? 1.3 : 0.4) * MiB), cache: 'HIT', key: `${repo}:${suite}:tr` },
  ]
  const plan = aptInstallPlan(repo)
  const n = Math.floor(between(3, 14))
  for (let i = 0; i < n; i++) {
    const p = pick(plan)
    const fetch = Math.random() < 0.25
    specs.push({ ...base, artifact: p.path.split('/').pop()!, path: p.path, component: p.name, size: p.size, cache: fetch ? 'FETCHING' : 'HIT', key: `${repo}:${p.path}` })
  }
  return { label: `apt install (${repo})`, client: randomClient([APT_AGENT]), concurrency: 3, specs }
}

/** A curl download of a release binary or an ISO. */
export function rawDownload(forceRepo?: string): JobPlan {
  const repo = forceRepo ?? (Math.random() < 0.75 ? 'raw-releases' : 'raw-isos')
  const files = RAW_FILES[repo]!.filter(([, size]) => size > 100 * KiB)
  const [path, size] = pick(files)
  const fetch = repo === 'raw-releases' && Math.random() < 0.2
  return {
    label: `curl ${path}`,
    client: randomClient([CURL_AGENT, 'Wget/1.25.0', 'Ansible-get_url/2.19']),
    concurrency: 1,
    specs: [{ repository: repo, format: 'raw', artifact: path.split('/').pop()!, path, component: null, size: Math.round(size), cache: fetch ? 'FETCHING' : 'HIT', key: `${repo}:${path}` }],
  }
}

/** Steady background: a single small metadata hit. */
export function trickle(): JobPlan {
  const kind = Math.random()
  if (kind < 0.4) {
    const seed = pick(DOCKER_IMAGES['docker-hub']!)
    const tag = pick(seed.tags)
    return { label: 'manifest HEAD', client: randomClient(DOCKER_AGENTS), concurrency: 1, specs: [{ repository: 'docker-hub', format: 'docker', artifact: `${seed.name}:${tag}`, path: `v2/${seed.name}/manifests/${tag}`, component: `${seed.name.replace(/^library\//, '')}:${tag}`, size: Math.round(between(1.5, 8) * KiB), cache: Math.random() < 0.7 ? 'HIT' : 'REVALIDATED', key: `trickle:${Math.random()}` }] }
  }
  const p = pick(NPM_PACKAGES)
  return { label: 'packument', client: randomClient(NPM_AGENTS), concurrency: 1, specs: [{ repository: 'npm-proxy', format: 'npm', artifact: `${p.name} (packument)`, path: p.name, component: null, size: Math.round(logBetween(6, 700) * KiB), cache: Math.random() < 0.6 ? 'HIT' : 'REVALIDATED', key: `trickle:${Math.random()}` }] }
}
