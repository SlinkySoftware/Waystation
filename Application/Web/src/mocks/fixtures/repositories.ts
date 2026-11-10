// SPDX-License-Identifier: GPL-3.0-or-later
// Seeded repositories: a home lab's proxies (two docker registries, Debian / Ubuntu / Raspberry Pi apt archives and
// their security pockets, the RHEL family via yum, npm, one raw example). Upstreams are the public canonical hosts;
// the instance itself is registry.example.com (no environment details, CLAUDE.md).
import type { QuotaMode, Repository, RepositoryQuota, StorageConfig } from '@/api/types'

import { GiB, hash, rng } from '../rng'

export const BASE_URL = 'https://registry.example.com'
export const REGISTRY_HOST = 'registry.example.com'

const NOW = Date.now()
const ago = (ms: number) => new Date(NOW - ms).toISOString()
const DAY = 86_400_000

interface Seed {
  name: string
  format: Repository['format']
  description: string
  upstream: string
  status?: Repository['status']
  sizeGiB: number
  artifacts: number
  hitRatio: number
  requests24h: number
  createdDaysAgo: number
  options?: Repository['format_options']
  /** Freshness, minutes (content: null = forever, immutable). */
  metadataMaxAge: number
  contentMaxAge: number | null
  negativeTtl: number
  /** Hours since the last request (default: within the last 90 s). */
  idleHours?: number
  /** Stored-bytes quota (GiB) and its mode; absent = no quota. */
  quota?: { gib: number; mode: QuotaMode }
}

/** Usage against a quota, as the server reports it (`warning` from 80 %, `exceeded` at the quota). */
export function quotaView(storage: StorageConfig | undefined, usedBytes: number): RepositoryQuota | null {
  const quota = storage?.quota_bytes
  if (!quota) return null
  const ratio = usedBytes / quota
  return {
    quota_bytes: quota,
    mode: storage.quota_mode ?? 'soft',
    used_bytes: usedBytes,
    used_ratio: Math.round(ratio * 10_000) / 10_000,
    state: ratio >= 1 ? 'exceeded' : ratio >= 0.8 ? 'warning' : 'ok',
  }
}

const DOCKER = { metadataMaxAge: 360, contentMaxAge: null, negativeTtl: 60 }
const DISTRO = { metadataMaxAge: 60, contentMaxAge: 1440, negativeTtl: 10 }
const SECURITY = { metadataMaxAge: 30, contentMaxAge: 1440, negativeTtl: 10 }
const OTHER = { metadataMaxAge: 30, contentMaxAge: 1440, negativeTtl: 10 }

const SEEDS: Seed[] = [
  {
    name: 'dockerhub-proxy',
    format: 'docker',
    description: 'Docker Hub pull-through cache (default docker repository: mirror mode and unprefixed names)',
    upstream: 'https://registry-1.docker.io',
    sizeGiB: 186.4,
    artifacts: 4812,
    hitRatio: 0.934,
    requests24h: 48_210,
    createdDaysAgo: 41,
    options: { aliases: ['docker.io'], docker_hub: true, default_repo: true },
    ...DOCKER,
  },
  {
    name: 'ghcr-proxy',
    format: 'docker',
    description: 'GitHub Container Registry (pull as ghcr.io/<owner>/<image>)',
    upstream: 'https://ghcr.io',
    sizeGiB: 31.6,
    artifacts: 1_164,
    hitRatio: 0.902,
    requests24h: 8_317,
    createdDaysAgo: 41,
    options: { aliases: ['ghcr.io'], docker_hub: false, default_repo: false },
    ...DOCKER,
  },
  {
    name: 'debian',
    format: 'apt',
    description: 'Debian archive (trixie, trixie-updates, trixie-backports)',
    upstream: 'https://deb.debian.org/debian/',
    sizeGiB: 38.2,
    artifacts: 18_744,
    hitRatio: 0.911,
    requests24h: 31_448,
    createdDaysAgo: 41,
    options: { distribution: 'trixie', flat: false },
    ...DISTRO,
  },
  {
    name: 'debian-security',
    format: 'apt',
    description: 'Debian security updates (trixie-security)',
    upstream: 'https://security.debian.org/debian-security/',
    sizeGiB: 4.4,
    artifacts: 2_206,
    hitRatio: 0.842,
    requests24h: 7_921,
    createdDaysAgo: 41,
    options: { distribution: 'trixie-security', flat: false },
    ...SECURITY,
  },
  {
    name: 'ubuntu',
    format: 'apt',
    description: 'Ubuntu archive (resolute, resolute-updates, resolute-backports)',
    upstream: 'http://archive.ubuntu.com/ubuntu/',
    sizeGiB: 29.8,
    artifacts: 14_102,
    hitRatio: 0.903,
    requests24h: 19_877,
    createdDaysAgo: 35,
    options: { distribution: 'resolute', flat: false },
    ...DISTRO,
  },
  {
    name: 'ubuntu-security',
    format: 'apt',
    description: 'Ubuntu security updates (resolute-security)',
    upstream: 'http://security.ubuntu.com/ubuntu/',
    sizeGiB: 3.1,
    artifacts: 1_538,
    hitRatio: 0.826,
    requests24h: 5_204,
    createdDaysAgo: 35,
    options: { distribution: 'resolute-security', flat: false },
    ...SECURITY,
  },
  {
    name: 'raspberrypi',
    format: 'apt',
    description: 'Raspberry Pi OS archive (trixie, arm64 and armhf)',
    upstream: 'https://archive.raspberrypi.com/debian/',
    sizeGiB: 3.2,
    artifacts: 1_481,
    hitRatio: 0.879,
    requests24h: 2_215,
    createdDaysAgo: 18,
    options: { distribution: 'trixie', flat: false },
    ...DISTRO,
  },
  {
    name: 'rocky',
    format: 'yum',
    description: 'Rocky Linux (BaseOS, AppStream, CRB)',
    upstream: 'https://dl.rockylinux.org/pub/rocky/',
    sizeGiB: 21.7,
    artifacts: 9_412,
    hitRatio: 0.917,
    requests24h: 12_386,
    createdDaysAgo: 33,
    ...DISTRO,
  },
  {
    name: 'almalinux',
    format: 'yum',
    description: 'AlmaLinux (BaseOS, AppStream, CRB)',
    upstream: 'https://repo.almalinux.org/almalinux/',
    sizeGiB: 14.3,
    artifacts: 6_087,
    hitRatio: 0.894,
    requests24h: 7_652,
    createdDaysAgo: 33,
    ...DISTRO,
  },
  {
    name: 'epel',
    format: 'yum',
    description: 'Extra Packages for Enterprise Linux (rarely used)',
    upstream: 'https://dl.fedoraproject.org/pub/epel/',
    status: 'blocked',
    sizeGiB: 1.2,
    artifacts: 214,
    hitRatio: 0.71,
    requests24h: 96,
    createdDaysAgo: 30,
    idleHours: 0.12,
    ...DISTRO,
  },
  {
    name: 'oracle',
    format: 'yum',
    description: 'Oracle Linux (baseos, appstream, UEK)',
    upstream: 'https://yum.oracle.com/',
    status: 'degraded',
    sizeGiB: 8.7,
    artifacts: 3_356,
    hitRatio: 0.862,
    requests24h: 3_911,
    createdDaysAgo: 27,
    ...DISTRO,
  },
  {
    name: 'npm',
    format: 'npm',
    description: 'npmjs.org registry',
    upstream: 'https://registry.npmjs.org',
    sizeGiB: 41.7,
    artifacts: 61_387,
    hitRatio: 0.962,
    requests24h: 214_655,
    createdDaysAgo: 41,
    options: { audit_passthrough: true },
    quota: { gib: 50, mode: 'soft' },
    ...OTHER,
  },
  {
    name: 'raw-releases',
    format: 'raw',
    description: 'Example raw proxy: GitHub release downloads (CLI tools, binaries)',
    upstream: 'https://github.com/',
    sizeGiB: 12.9,
    artifacts: 388,
    hitRatio: 0.948,
    requests24h: 1_604,
    createdDaysAgo: 29,
    options: { metadata_path_regex: null, content_disposition: 'attachment' },
    quota: { gib: 20, mode: 'hard' },
    ...OTHER,
  },
]

/** Average response size per request, by format (bytes): drives bytes served from request counts. */
const BYTES_PER_REQUEST: Partial<Record<Repository['format'], number>> = { npm: 180_000, docker: 9_500_000, raw: 40_000_000, yum: 2_100_000 }

/** Requests per hour for the last 24 h: diurnal shape, seeded noise, scaled to the 24 h total. */
function spark(name: string, total: number): number[] {
  const next = rng(hash(`spark:${name}`))
  const hourNow = new Date(NOW).getHours()
  const raw = Array.from({ length: 24 }, (_, i) => {
    const hour = (hourNow - 23 + i + 24) % 24
    const diurnal = 0.25 + Math.max(0, Math.sin(((hour - 6) / 24) * 2 * Math.PI)) * 1.1
    const burst = next() < 0.12 ? 1.8 + next() * 2 : 1
    return diurnal * burst * (0.75 + next() * 0.5)
  })
  const sum = raw.reduce((a, b) => a + b, 0)
  return raw.map((v) => Math.round((v / sum) * total))
}

/** Repositories the demo's provisioning file declares (T-O09): shown with the "Managed" label. */
export const PROVISIONED = new Set(['dockerhub-proxy', 'ghcr-proxy', 'npm'])

export function seedRepositories(): Repository[] {
  return SEEDS.map((s) => {
    const next = rng(hash(`repo:${s.name}`))
    const storage: StorageConfig = { quota_bytes: s.quota ? s.quota.gib * GiB : null, quota_mode: s.quota?.mode ?? 'soft' }
    const bytesServed = Math.round(s.requests24h * (BYTES_PER_REQUEST[s.format] ?? 1_400_000) * (0.7 + next() * 0.6))
    return {
      name: s.name,
      format: s.format,
      type: 'proxy',
      online: true,
      description: s.description,
      status: s.status ?? 'ok',
      url: `${BASE_URL}/repository/${s.name}/`,
      proxy: {
        upstream_url: s.upstream,
        auth: s.name === 'dockerhub-proxy' ? { kind: 'basic', username: 'example-hub-user', secret_set: true } : { kind: 'none' },
        metadata_max_age_s: s.metadataMaxAge * 60,
        content_max_age_s: s.contentMaxAge === null ? null : s.contentMaxAge * 60,
        negative_ttl_s: s.negativeTtl * 60,
        stale_on_error: true,
        offline: false,
        allow_private_upstream: false,
        ca_pem: null,
        http_proxy: null,
        auto_block: { enabled: true, failures: 5, initial_s: 30, max_s: 1800 },
      },
      ...(s.options ? { format_options: s.options } : {}),
      managed_by: PROVISIONED.has(s.name) ? 'provisioning' : null,
      storage,
      quota: quotaView(storage, Math.round(s.sizeGiB * GiB)),
      created_at: ago(s.createdDaysAgo * DAY),
      updated_at: ago(Math.round(next() * 9 * DAY)),
      stats: {
        size_bytes: Math.round(s.sizeGiB * GiB),
        artifact_count: s.artifacts,
        hit_ratio_24h: s.hitRatio,
        requests_24h: s.requests24h,
        bytes_served_24h: bytesServed,
        bytes_saved_24h: Math.round(bytesServed * s.hitRatio),
        requests_spark_24h: spark(s.name, s.requests24h),
        last_activity_at: s.idleHours ? ago(s.idleHours * 3_600_000) : ago(Math.round(next() * 90_000)),
      },
    }
  })
}
