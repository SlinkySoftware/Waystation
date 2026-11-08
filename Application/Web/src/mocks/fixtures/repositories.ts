// SPDX-License-Identifier: GPL-3.0-or-later
// Seeded repositories: the proxies a home lab typically runs. Upstreams are the real public registries;
// the instance itself is registry.example.com (no environment details, CLAUDE.md).
import type { Repository } from '@/api/types'

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
  online?: boolean
  sizeGiB: number
  artifacts: number
  hitRatio: number
  requests24h: number
  createdDaysAgo: number
  options?: Repository['format_options']
  offline?: boolean
  metadataMaxAge?: number
}

const SEEDS: Seed[] = [
  {
    name: 'docker-hub',
    format: 'docker',
    description: 'Docker Hub pull-through cache (default docker repository, mirror mode)',
    upstream: 'https://registry-1.docker.io',
    sizeGiB: 186.4,
    artifacts: 4812,
    hitRatio: 0.934,
    requests24h: 48_210,
    createdDaysAgo: 41,
    options: { aliases: ['docker.io'], docker_hub: true, default_repo: true },
  },
  {
    name: 'ghcr',
    format: 'docker',
    description: 'GitHub Container Registry',
    upstream: 'https://ghcr.io',
    sizeGiB: 23.4,
    artifacts: 911,
    hitRatio: 0.887,
    requests24h: 6_904,
    createdDaysAgo: 41,
    options: { aliases: ['ghcr.io'], docker_hub: false, default_repo: false },
  },
  {
    name: 'quay',
    format: 'docker',
    description: 'Red Hat Quay',
    upstream: 'https://quay.io',
    status: 'degraded',
    sizeGiB: 6.1,
    artifacts: 233,
    hitRatio: 0.71,
    requests24h: 1_312,
    createdDaysAgo: 30,
    options: { aliases: ['quay.io'], docker_hub: false, default_repo: false },
  },
  {
    name: 'k8s-registry',
    format: 'docker',
    description: 'registry.k8s.io (Kubernetes images)',
    upstream: 'https://registry.k8s.io',
    status: 'blocked',
    sizeGiB: 3.8,
    artifacts: 164,
    hitRatio: 0.96,
    requests24h: 512,
    createdDaysAgo: 22,
    options: { aliases: ['registry.k8s.io'], docker_hub: false, default_repo: false },
  },
  {
    name: 'npm-proxy',
    format: 'npm',
    description: 'npmjs.org registry',
    upstream: 'https://registry.npmjs.org',
    sizeGiB: 41.7,
    artifacts: 61_387,
    hitRatio: 0.962,
    requests24h: 214_655,
    createdDaysAgo: 41,
    options: { audit_passthrough: true },
  },
  {
    name: 'debian',
    format: 'apt',
    description: 'Debian main archive (trixie, trixie-updates, trixie-backports)',
    upstream: 'https://deb.debian.org/debian',
    sizeGiB: 38.2,
    artifacts: 18_744,
    hitRatio: 0.911,
    requests24h: 31_448,
    createdDaysAgo: 41,
    options: { distribution: 'trixie', flat: false },
  },
  {
    name: 'debian-security',
    format: 'apt',
    description: 'Debian security updates',
    upstream: 'https://security.debian.org/debian-security',
    sizeGiB: 4.4,
    artifacts: 2_206,
    hitRatio: 0.842,
    requests24h: 7_921,
    createdDaysAgo: 41,
    options: { distribution: 'trixie-security', flat: false },
    metadataMaxAge: 600,
  },
  {
    name: 'ubuntu',
    format: 'apt',
    description: 'Ubuntu archive (noble, noble-updates)',
    upstream: 'http://archive.ubuntu.com/ubuntu',
    sizeGiB: 29.8,
    artifacts: 14_102,
    hitRatio: 0.903,
    requests24h: 19_877,
    createdDaysAgo: 35,
    options: { distribution: 'noble', flat: false },
  },
  {
    name: 'raspberrypi',
    format: 'apt',
    description: 'Raspberry Pi OS archive',
    upstream: 'https://archive.raspberrypi.com/debian',
    sizeGiB: 3.2,
    artifacts: 1_481,
    hitRatio: 0.879,
    requests24h: 2_215,
    createdDaysAgo: 18,
    options: { distribution: 'trixie', flat: false },
  },
  {
    name: 'raw-releases',
    format: 'raw',
    description: 'GitHub release downloads (CLI tools, binaries)',
    upstream: 'https://github.com',
    sizeGiB: 12.9,
    artifacts: 388,
    hitRatio: 0.948,
    requests24h: 1_604,
    createdDaysAgo: 29,
    options: { metadata_path_regex: null, content_disposition: 'attachment' },
  },
  {
    name: 'raw-isos',
    format: 'raw',
    description: 'Installer images from cdimage.debian.org (offline: serves only what is cached)',
    upstream: 'https://cdimage.debian.org',
    status: 'offline',
    offline: true,
    sizeGiB: 48.3,
    artifacts: 37,
    hitRatio: 1,
    requests24h: 18,
    createdDaysAgo: 40,
    options: { metadata_path_regex: '.*/SHA(256|512)SUMS$', content_disposition: 'attachment' },
  },
]

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

export function seedRepositories(): Repository[] {
  return SEEDS.map((s) => {
    const next = rng(hash(`repo:${s.name}`))
    const bytesServed = Math.round(s.requests24h * (s.format === 'npm' ? 180_000 : s.format === 'docker' ? 9_500_000 : s.format === 'raw' ? 40_000_000 : 1_400_000) * (0.7 + next() * 0.6))
    return {
      name: s.name,
      format: s.format,
      type: 'proxy',
      online: s.online ?? true,
      description: s.description,
      status: s.status ?? 'ok',
      url: `${BASE_URL}/repository/${s.name}/`,
      proxy: {
        upstream_url: s.upstream,
        auth: s.name === 'docker-hub' ? { kind: 'basic', username: 'example-hub-user', secret_set: true } : { kind: 'none' },
        metadata_max_age_s: s.metadataMaxAge ?? 1800,
        content_max_age_s: null,
        negative_ttl_s: 600,
        stale_on_error: true,
        offline: s.offline ?? false,
        allow_private_upstream: false,
      },
      format_options: s.options,
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
        last_activity_at: s.offline ? ago(5 * 3_600_000) : ago(Math.round(next() * 90_000)),
      },
    }
  })
}
