// SPDX-License-Identifier: GPL-3.0-or-later
// Repository form model: a flat, form-friendly shape that maps to and from the API's RepositoryWrite.
import type { AptOptions, DockerOptions, Format, M0Format, NpmOptions, RawOptions, Repository, RepositoryWrite } from '@/api/types'

/** Formats the form can hold: the creatable M0 formats, plus yum for editing existing repositories (no options yet). */
export type FormFormat = M0Format | 'yum'

export interface RepoFormModel {
  name: string
  format: FormFormat
  description: string
  online: boolean
  upstreamUrl: string
  authKind: 'none' | 'basic' | 'bearer'
  username: string
  /** Empty = keep the stored secret (edit) / none (create). Never pre-filled: secrets are write-only. */
  secret: string
  secretSet: boolean
  metadataMaxAgeMin: number
  negativeTtlMin: number
  /** Not edited in this form yet; carried through so saving keeps it. */
  contentMaxAgeS: number | null
  staleOnError: boolean
  offline: boolean
  allowPrivate: boolean
  // docker
  aliases: string[]
  dockerHub: boolean
  defaultRepo: boolean
  // npm
  auditPassthrough: boolean
  // apt
  distribution: string
  flat: boolean
  // raw
  metadataRegex: string
  contentDisposition: 'inline' | 'attachment'
}

export interface Preset {
  id: string
  label: string
  apply: Partial<RepoFormModel>
}

export const PRESETS: Record<M0Format, Preset[]> = {
  docker: [
    { id: 'hub', label: 'Docker Hub', apply: { name: 'docker-hub', upstreamUrl: 'https://registry-1.docker.io', aliases: ['docker.io'], dockerHub: true } },
    { id: 'ghcr', label: 'GitHub (ghcr.io)', apply: { name: 'ghcr', upstreamUrl: 'https://ghcr.io', aliases: ['ghcr.io'], dockerHub: false } },
    { id: 'quay', label: 'Quay', apply: { name: 'quay', upstreamUrl: 'https://quay.io', aliases: ['quay.io'], dockerHub: false } },
    { id: 'gcr', label: 'Google (gcr.io)', apply: { name: 'gcr', upstreamUrl: 'https://gcr.io', aliases: ['gcr.io'], dockerHub: false } },
  ],
  npm: [{ id: 'npmjs', label: 'npmjs.org', apply: { name: 'npm-proxy', upstreamUrl: 'https://registry.npmjs.org' } }],
  apt: [
    { id: 'debian', label: 'Debian', apply: { name: 'debian', upstreamUrl: 'https://deb.debian.org/debian', distribution: 'trixie' } },
    { id: 'debsec', label: 'Debian security', apply: { name: 'debian-security', upstreamUrl: 'https://security.debian.org/debian-security', distribution: 'trixie-security', metadataMaxAgeMin: 10 } },
    { id: 'ubuntu', label: 'Ubuntu', apply: { name: 'ubuntu', upstreamUrl: 'http://archive.ubuntu.com/ubuntu', distribution: 'resolute' } },
    { id: 'rpi', label: 'Raspberry Pi OS', apply: { name: 'raspberrypi', upstreamUrl: 'https://archive.raspberrypi.com/debian', distribution: 'trixie' } },
  ],
  raw: [{ id: 'github', label: 'GitHub releases', apply: { name: 'raw-github', upstreamUrl: 'https://github.com' } }],
}

export function emptyModel(format: FormFormat): RepoFormModel {
  return {
    name: '',
    format,
    description: '',
    online: true,
    upstreamUrl: '',
    authKind: 'none',
    username: '',
    secret: '',
    secretSet: false,
    metadataMaxAgeMin: 30,
    negativeTtlMin: 10,
    contentMaxAgeS: null,
    staleOnError: true,
    offline: false,
    allowPrivate: false,
    aliases: [],
    dockerHub: false,
    defaultRepo: false,
    auditPassthrough: true,
    distribution: '',
    flat: false,
    metadataRegex: '',
    contentDisposition: 'attachment',
  }
}

export function modelFromRepository(repo: Repository): RepoFormModel {
  const m = emptyModel(repo.format as FormFormat)
  const p = repo.proxy
  const o = (repo.format_options ?? {}) as DockerOptions & NpmOptions & Partial<AptOptions> & RawOptions
  return {
    ...m,
    name: repo.name,
    description: repo.description ?? '',
    online: repo.online,
    upstreamUrl: p?.upstream_url ?? '',
    authKind: p?.auth?.kind ?? 'none',
    username: p?.auth?.username ?? '',
    secretSet: Boolean(p?.auth?.secret_set),
    metadataMaxAgeMin: Math.round((p?.metadata_max_age_s ?? 1800) / 60),
    negativeTtlMin: Math.round((p?.negative_ttl_s ?? 600) / 60),
    contentMaxAgeS: p?.content_max_age_s ?? null,
    staleOnError: p?.stale_on_error ?? true,
    offline: p?.offline ?? false,
    allowPrivate: p?.allow_private_upstream ?? false,
    aliases: o.aliases ?? [],
    dockerHub: o.docker_hub ?? false,
    defaultRepo: o.default_repo ?? false,
    auditPassthrough: o.audit_passthrough ?? true,
    distribution: o.distribution ?? '',
    flat: o.flat ?? false,
    metadataRegex: o.metadata_path_regex ?? '',
    contentDisposition: o.content_disposition ?? 'attachment',
  }
}

function formatOptions(m: RepoFormModel): RepositoryWrite['format_options'] {
  switch (m.format) {
    case 'docker':
      return { aliases: m.aliases, docker_hub: m.dockerHub, default_repo: m.defaultRepo }
    case 'npm':
      return { audit_passthrough: m.auditPassthrough }
    case 'apt':
      return { distribution: m.distribution.trim(), flat: m.flat }
    case 'yum':
      return undefined
    case 'raw':
    default:
      return { metadata_path_regex: m.metadataRegex.trim() || null, content_disposition: m.contentDisposition }
  }
}

export function toWrite(m: RepoFormModel): RepositoryWrite {
  return {
    name: m.name.trim(),
    format: m.format as Format,
    type: 'proxy',
    online: m.online,
    description: m.description.trim(),
    proxy: {
      upstream_url: m.upstreamUrl.trim(),
      auth: {
        kind: m.authKind,
        username: m.authKind === 'basic' ? m.username.trim() : null,
        // Write-only: only sent when the user typed one.
        ...(m.authKind !== 'none' && m.secret ? { secret: m.secret } : {}),
      },
      metadata_max_age_s: Math.max(0, Math.round(m.metadataMaxAgeMin * 60)),
      content_max_age_s: m.contentMaxAgeS,
      negative_ttl_s: Math.max(0, Math.round(m.negativeTtlMin * 60)),
      stale_on_error: m.staleOnError,
      offline: m.offline,
      allow_private_upstream: m.allowPrivate,
    },
    ...(m.format === 'yum' ? {} : { format_options: formatOptions(m) }),
  }
}

/** Client-side checks mirroring the server's (the server stays authoritative). */
export const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$/
