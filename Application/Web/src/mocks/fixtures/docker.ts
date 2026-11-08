// SPDX-License-Identifier: GPL-3.0-or-later
// Docker images, tags and manifests for the docker proxies. Real public image names; sizes are plausible,
// digests are seeded fakes.
import type { DockerImage, DockerLayer, DockerManifest, DockerTag, Platform } from '@/api/types'

import { fakeSha256, hash, KiB, MiB, rng } from '../rng'
import { REGISTRY_HOST } from './repositories'

const NOW = Date.now()
const HOUR = 3_600_000

const OFFICIAL: Platform[] = [
  { os: 'linux', architecture: 'amd64', variant: null },
  { os: 'linux', architecture: 'arm64', variant: 'v8' },
  { os: 'linux', architecture: 'arm', variant: 'v7' },
  { os: 'linux', architecture: '386', variant: null },
  { os: 'linux', architecture: 'ppc64le', variant: null },
  { os: 'linux', architecture: 's390x', variant: null },
]
const TWO: Platform[] = OFFICIAL.slice(0, 2)
const ONE: Platform[] = OFFICIAL.slice(0, 1)

interface ImageSeed {
  name: string
  tags: string[]
  /** Approximate compressed size of the amd64 image, MiB. */
  mib: number
  layers: number
  platforms: Platform[]
  /** History `created_by` for the top layers, newest last. */
  history?: string[]
}

const DEBIAN_BASE = 'ADD rootfs.tar.xz / # buildkit'
const ALPINE_BASE = 'ADD alpine-minirootfs-3.22.2-x86_64.tar.gz / # buildkit'

export const DOCKER_IMAGES: Record<string, ImageSeed[]> = {
  'dockerhub-proxy': [
    { name: 'library/nginx', tags: ['1.29.3', '1.29', 'mainline', 'latest', '1.28.0', 'stable', '1.29.3-alpine', 'alpine'], mib: 68.4, layers: 7, platforms: OFFICIAL, history: [DEBIAN_BASE, 'ENV NGINX_VERSION=1.29.3', 'RUN /bin/sh -c set -x && groupadd --system --gid 101 nginx && apt-get update && apt-get install …', 'COPY docker-entrypoint.sh / # buildkit', 'COPY 10-listen-on-ipv6-by-default.sh /docker-entrypoint.d # buildkit', 'COPY 20-envsubst-on-templates.sh /docker-entrypoint.d # buildkit', 'COPY 30-tune-worker-processes.sh /docker-entrypoint.d # buildkit'] },
    { name: 'library/postgres', tags: ['18.1', '18', 'latest', '17.7', '17', '16.11'], mib: 151.2, layers: 14, platforms: OFFICIAL },
    { name: 'library/python', tags: ['3.14-slim', '3.14.0-slim-trixie', '3.13-slim', '3.14'], mib: 41.7, layers: 4, platforms: OFFICIAL },
    { name: 'library/node', tags: ['26-slim', '24-alpine', '24', 'lts-slim'], mib: 72.9, layers: 5, platforms: TWO },
    { name: 'library/alpine', tags: ['3.22', '3.22.2', 'latest', '3.21'], mib: 3.6, layers: 1, platforms: OFFICIAL, history: [ALPINE_BASE] },
    { name: 'library/debian', tags: ['trixie-slim', 'trixie', '13', 'bookworm-slim'], mib: 28.2, layers: 1, platforms: OFFICIAL, history: [DEBIAN_BASE] },
    { name: 'library/ubuntu', tags: ['26.04', 'resolute', 'latest', '24.04'], mib: 29.4, layers: 1, platforms: TWO },
    { name: 'library/almalinux', tags: ['10', '10.1', '10-minimal', '9'], mib: 72.6, layers: 1, platforms: TWO },
    { name: 'library/oraclelinux', tags: ['9', '9-slim', '10'], mib: 81.9, layers: 1, platforms: TWO },
    { name: 'library/eclipse-mosquitto', tags: ['2.0.22', '2', 'latest'], mib: 10.3, layers: 3, platforms: OFFICIAL },
    { name: 'pihole/pihole', tags: ['2025.11.0', 'latest'], mib: 41.8, layers: 8, platforms: OFFICIAL },
    { name: 'library/golang', tags: ['1.25', '1.25.4-trixie', '1.25-alpine'], mib: 285.3, layers: 6, platforms: TWO },
    { name: 'library/mongo', tags: ['8.0', '8.0.15', '7.0'], mib: 260.8, layers: 10, platforms: TWO },
    { name: 'library/traefik', tags: ['v3.5', 'v3.5.4', 'latest'], mib: 48.6, layers: 4, platforms: TWO },
    { name: 'library/busybox', tags: ['1.37', 'latest', 'musl'], mib: 2.1, layers: 1, platforms: OFFICIAL },
    { name: 'library/eclipse-temurin', tags: ['21-jre', '25-jdk', '21-jdk'], mib: 96.4, layers: 5, platforms: TWO },
    { name: 'valkey/valkey', tags: ['8.1', '8.1.4', '8', 'latest'], mib: 44.2, layers: 6, platforms: OFFICIAL },
    { name: 'grafana/grafana', tags: ['12.2.1', '12.2', 'latest'], mib: 186.5, layers: 11, platforms: TWO },
    { name: 'prom/prometheus', tags: ['v3.7.3', 'latest'], mib: 112.8, layers: 9, platforms: TWO },
    { name: 'gitea/act_runner', tags: ['0.2.13', 'latest'], mib: 18.4, layers: 5, platforms: TWO },
  ],
  'ghcr-proxy': [
    { name: 'home-assistant/home-assistant', tags: ['2026.11', 'stable', '2026.10.4'], mib: 642.7, layers: 18, platforms: TWO },
    { name: 'immich-app/immich-server', tags: ['v2.2.3', 'release', 'v2.2.2'], mib: 734.9, layers: 21, platforms: TWO },
    { name: 'immich-app/immich-machine-learning', tags: ['v2.2.3', 'release'], mib: 1106.2, layers: 14, platforms: TWO },
    { name: 'paperless-ngx/paperless-ngx', tags: ['2.19.5', 'latest'], mib: 581.4, layers: 16, platforms: TWO },
    { name: 'open-webui/open-webui', tags: ['v0.6.36', 'main'], mib: 1720.6, layers: 19, platforms: TWO },
    { name: 'gethomepage/homepage', tags: ['v1.7.0', 'latest'], mib: 92.3, layers: 9, platforms: TWO },
    { name: 'linuxserver/jellyfin', tags: ['10.11.2', 'latest'], mib: 297.1, layers: 9, platforms: TWO },
    { name: 'goauthentik/server', tags: ['2025.10.1', '2025.10'], mib: 344.6, layers: 12, platforms: TWO },
    { name: 'astral-sh/uv', tags: ['0.9.8', 'latest', 'python3.14-trixie-slim'], mib: 19.8, layers: 2, platforms: TWO },
    { name: 'actions/actions-runner', tags: ['2.329.0', 'latest'], mib: 412.3, layers: 13, platforms: ONE },
  ],
}

/** How a docker repository is addressed in pull commands: unprefixed for the default repo, by alias, else by name. */
export const PULL_PREFIX: Record<string, string> = { 'dockerhub-proxy': '', 'ghcr-proxy': 'ghcr.io/' }

function platformKey(p: Platform): string {
  return `${p.os}/${p.architecture}${p.variant ? `/${p.variant}` : ''}`
}

/** Split a total into layer sizes: one big base layer, a couple of medium ones, small config/script layers. */
function layerSizes(seed: ImageSeed, platform: Platform): number[] {
  const next = rng(hash(`${seed.name}:${platformKey(platform)}`))
  const archScale = platform.architecture === 'amd64' ? 1 : 0.88 + next() * 0.2
  const total = seed.mib * MiB * archScale
  if (seed.layers === 1) return [Math.round(total)]
  const weights = Array.from({ length: seed.layers }, (_, i) => (i === 0 ? 5 + next() * 3 : i < 3 ? 1 + next() * 2.5 : 0.002 + next() * 0.02))
  const sum = weights.reduce((a, b) => a + b, 0)
  return weights.map((w) => Math.max(Math.round(180 + next() * 900), Math.round((w / sum) * total)))
}

function layersFor(seed: ImageSeed, platform: Platform, tag: string): DockerLayer[] {
  return layerSizes(seed, platform).map((size, i) => ({
    digest: `sha256:${fakeSha256(`${seed.name}:${platformKey(platform)}:${i}:${tag.replace(/[^0-9.]/g, '').slice(0, 4)}`)}`,
    size,
    media_type: 'application/vnd.oci.image.layer.v1.tar+gzip',
    cached: platform.architecture === 'amd64' || platform.architecture === 'arm64' || i === 0,
    created_by: seed.history?.[i] ?? (i === 0 ? DEBIAN_BASE : `RUN /bin/sh -c #(nop) layer ${i + 1}`),
  }))
}

export function dockerImagesFor(repo: string): DockerImage[] {
  return (DOCKER_IMAGES[repo] ?? []).map((seed) => {
    const next = rng(hash(`img:${repo}:${seed.name}`))
    return {
      name: seed.name,
      tag_count: seed.tags.length,
      size_bytes: Math.round(seed.mib * MiB * (1.6 + next() * 1.4)),
      last_pulled_at: new Date(NOW - next() * 30 * HOUR).toISOString(),
      pulls: Math.round(20 + next() * 4000),
    }
  })
}

function findSeed(repo: string, image: string): ImageSeed | undefined {
  return DOCKER_IMAGES[repo]?.find((s) => s.name === image)
}

export function dockerTagsFor(repo: string, image: string): DockerTag[] | null {
  const seed = findSeed(repo, image)
  if (!seed) return null
  return seed.tags.map((tag, i) => {
    const next = rng(hash(`tag:${repo}:${image}:${tag}`))
    const platforms = tag.includes('alpine') ? seed.platforms.slice(0, 4) : seed.platforms
    const size = layerSizes(seed, seed.platforms[0]!).reduce((a, b) => a + b, 0) * (tag.includes('alpine') ? 0.38 : 1)
    return {
      tag,
      digest: `sha256:${fakeSha256(`index:${image}:${tag.replace(/[^0-9.]/g, '').slice(0, 4) || tag}`)}`,
      media_type: platforms.length > 1 ? 'application/vnd.oci.image.index.v1+json' : 'application/vnd.oci.image.manifest.v1+json',
      platforms,
      size_bytes: Math.round(size),
      created_at: new Date(NOW - (i + 1) * (2 + next() * 9) * 24 * HOUR).toISOString(),
      updated_at: new Date(NOW - (i * 11 + next() * 30) * HOUR).toISOString(),
      last_pulled_at: new Date(NOW - next() * 48 * HOUR).toISOString(),
      pulls: Math.round(5 + next() * 900 / (i + 1)),
    }
  })
}

export function dockerManifestFor(repo: string, image: string, reference: string, platform?: string): DockerManifest | null {
  const seed = findSeed(repo, image)
  if (!seed) return null
  const tags = dockerTagsFor(repo, image) ?? []
  const tag = tags.find((t) => t.tag === reference || t.digest === reference)
  if (!tag) return null
  const chosen = tag.platforms.find((p) => platformKey(p) === platform) ?? tag.platforms[0]!
  const layers = layersFor(seed, chosen, tag.tag)
  const isIndex = tag.platforms.length > 1
  const platformDigest = `sha256:${fakeSha256(`manifest:${image}:${tag.tag}:${platformKey(chosen)}`)}`
  return {
    image,
    reference,
    digest: tag.digest,
    media_type: tag.media_type,
    is_index: isIndex,
    manifests: isIndex
      ? tag.platforms.map((p) => ({ platform: p, digest: `sha256:${fakeSha256(`manifest:${image}:${tag.tag}:${platformKey(p)}`)}`, size: 1_600 + (hash(platformKey(p)) % 900) }))
      : [],
    platform: chosen,
    platform_digest: isIndex ? platformDigest : tag.digest,
    config_digest: `sha256:${fakeSha256(`config:${image}:${tag.tag}:${platformKey(chosen)}`)}`,
    created_at: tag.created_at ?? null,
    layers,
    total_size: layers.reduce((a, l) => a + l.size, 0),
    labels: image.startsWith('library/')
      ? { maintainer: 'Docker Official Images', 'org.opencontainers.image.version': tag.tag }
      : { 'org.opencontainers.image.source': `https://github.com/${image}`, 'org.opencontainers.image.version': tag.tag },
    pull_by_tag: `docker pull ${REGISTRY_HOST}/${PULL_PREFIX[repo] ?? `${repo}/`}${image.replace(/^library\//, '')}:${tag.tag}`,
    pull_by_digest: `docker pull ${REGISTRY_HOST}/${PULL_PREFIX[repo] ?? `${repo}/`}${image.replace(/^library\//, '')}@${tag.digest}`,
  }
}

/** Layer sizes of a tag's amd64 image (used by the live generator for realistic pulls). */
export function pullPlan(repo: string, image: string, tag: string): { digest: string; size: number }[] {
  const seed = findSeed(repo, image)
  if (!seed) return []
  return layersFor(seed, seed.platforms[0]!, tag).map((l) => ({ digest: l.digest, size: l.size }))
}

export const SMALL_CONFIG_BLOB = 7 * KiB
