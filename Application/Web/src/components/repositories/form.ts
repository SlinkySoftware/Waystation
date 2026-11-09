// SPDX-License-Identifier: GPL-3.0-or-later
// Repository form model: a flat, form-friendly shape that maps to and from the API's RepositoryWrite.
import type { AptOptions, DockerOptions, Format, GoOptions, M0Format, MavenOptions, NpmOptions, PypiOptions, QuotaMode, RawOptions, Repository, RepositoryWrite, WritePolicy, YumOptions } from '@/api/types'
import { fromBytes, type QuotaUnit, toBytes } from '@/utils/quota'

/** Formats the form can hold: the creatable M0 formats, plus yum for editing existing repositories (options carried through). */
export type FormFormat = M0Format | 'yum'

/** Repository types the form can create or edit: proxies for every format; hosted and group where the format serves them. */
export type FormType = 'proxy' | 'hosted' | 'group'

/** Formats whose hosted repositories are served (the create flow enables the "Hosted" tile for these). */
export const HOSTED_FORMATS: readonly FormFormat[] = ['docker', 'raw', 'maven', 'pypi', 'helm']
/** Formats whose group repositories are served (the create flow enables the "Group" tile for these). */
export const GROUP_FORMATS: readonly FormFormat[] = ['docker', 'raw', 'maven', 'pypi', 'go', 'helm']
/** Formats whose groups may name a hosted writable member (uploads sent to the group land there). */
export const WRITABLE_GROUP_FORMATS: readonly FormFormat[] = ['docker', 'raw', 'pypi', 'helm']

export interface RepoFormModel {
  name: string
  format: FormFormat
  type: FormType
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
  // hosted
  writePolicy: WritePolicy
  immutableTags: string[]
  // group: member names in resolution order, and the hosted member that receives pushes (docker)
  members: string[]
  writableMember: string | null
  /** Extra trusted CA certificates (PEM) for this upstream; empty = system CAs only. */
  caPem: string
  /** Outbound HTTP proxy for this upstream; empty = the server-wide proxy. */
  httpProxy: string
  autoBlock: boolean
  autoBlockFailures: number
  autoBlockInitialS: number
  autoBlockMaxMin: number
  // storage quota (proxy and hosted; groups store nothing)
  quotaEnabled: boolean
  quotaSize: number
  quotaUnit: QuotaUnit
  quotaMode: QuotaMode
  // docker
  aliases: string[]
  dockerHub: boolean
  defaultRepo: boolean
  // npm
  auditPassthrough: boolean
  // pypi
  jsonApi: boolean
  // go (checksum database)
  sumdbProxy: boolean
  sumdbName: string
  sumdbUrl: string
  verifyChecksums: boolean
  nosumdb: string[]
  // apt
  distribution: string
  flat: boolean
  // raw
  metadataRegex: string
  contentDisposition: 'inline' | 'attachment'
  // maven
  versionPolicy: NonNullable<MavenOptions['version_policy']>
  layoutPolicy: NonNullable<MavenOptions['layout_policy']>
  // yum: not edited in this form yet; carried through so saving keeps them
  repoPath: string
  gpgKey: string | null
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
  helm: [
    { id: 'prometheus', label: 'Prometheus community', apply: { name: 'prometheus-community', upstreamUrl: 'https://prometheus-community.github.io/helm-charts' } },
    { id: 'jetstack', label: 'Jetstack (cert-manager)', apply: { name: 'jetstack', upstreamUrl: 'https://charts.jetstack.io' } },
    { id: 'ingress-nginx', label: 'ingress-nginx', apply: { name: 'ingress-nginx', upstreamUrl: 'https://kubernetes.github.io/ingress-nginx' } },
  ],
  maven: [
    { id: 'central', label: 'Maven Central', apply: { name: 'maven-central', upstreamUrl: 'https://repo.maven.apache.org/maven2/' } },
    { id: 'google', label: 'Google Maven', apply: { name: 'maven-google', upstreamUrl: 'https://maven.google.com/' } },
    { id: 'gradle', label: 'Gradle plugins', apply: { name: 'gradle-plugins', upstreamUrl: 'https://plugins.gradle.org/m2/' } },
  ],
  pypi: [{ id: 'pypi', label: 'pypi.org', apply: { name: 'pypi-proxy', upstreamUrl: 'https://pypi.org' } }],
  go: [{ id: 'goproxy', label: 'proxy.golang.org', apply: { name: 'go-proxy', upstreamUrl: 'https://proxy.golang.org' } }],
}

export function emptyModel(format: FormFormat, type: FormType = 'proxy'): RepoFormModel {
  return {
    name: '',
    format,
    type,
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
    writePolicy: 'allow_redeploy',
    immutableTags: [],
    members: [],
    writableMember: null,
    caPem: '',
    httpProxy: '',
    autoBlock: true,
    autoBlockFailures: 5,
    autoBlockInitialS: 30,
    autoBlockMaxMin: 30,
    quotaEnabled: false,
    quotaSize: 50,
    quotaUnit: 'GiB',
    quotaMode: 'soft',
    aliases: [],
    dockerHub: false,
    defaultRepo: false,
    auditPassthrough: true,
    jsonApi: true,
    sumdbProxy: true,
    sumdbName: 'sum.golang.org',
    sumdbUrl: 'https://sum.golang.org',
    verifyChecksums: true,
    nosumdb: [],
    distribution: '',
    flat: false,
    metadataRegex: '',
    contentDisposition: 'attachment',
    versionPolicy: 'release',
    layoutPolicy: 'strict',
    repoPath: '',
    gpgKey: null,
  }
}

export function modelFromRepository(repo: Repository): RepoFormModel {
  const m = emptyModel(repo.format as FormFormat, repo.type)
  const p = repo.proxy
  const h = repo.hosted
  const quotaBytes = repo.storage?.quota_bytes ?? null
  const quotaSize = quotaBytes ? fromBytes(quotaBytes) : { size: m.quotaSize, unit: m.quotaUnit }
  const o = (repo.format_options ?? {}) as DockerOptions & NpmOptions & Partial<AptOptions> & RawOptions & YumOptions & MavenOptions & PypiOptions & GoOptions
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
    writePolicy: h?.write_policy ?? 'allow_redeploy',
    immutableTags: h?.immutable_tag_patterns ?? [],
    members: repo.group?.members ?? [],
    writableMember: repo.group?.writable_member ?? null,
    caPem: p?.ca_pem ?? '',
    httpProxy: p?.http_proxy ?? '',
    autoBlock: p?.auto_block?.enabled ?? true,
    autoBlockFailures: p?.auto_block?.failures ?? 5,
    autoBlockInitialS: p?.auto_block?.initial_s ?? 30,
    autoBlockMaxMin: Math.round((p?.auto_block?.max_s ?? 1800) / 60),
    quotaEnabled: quotaBytes !== null,
    quotaSize: quotaSize.size,
    quotaUnit: quotaSize.unit,
    quotaMode: repo.storage?.quota_mode ?? 'soft',
    aliases: o.aliases ?? [],
    dockerHub: o.docker_hub ?? false,
    defaultRepo: o.default_repo ?? false,
    auditPassthrough: o.audit_passthrough ?? true,
    jsonApi: o.json_api ?? true,
    sumdbProxy: o.sumdb_proxy ?? true,
    sumdbName: o.sumdb_name ?? 'sum.golang.org',
    sumdbUrl: o.sumdb_url ?? 'https://sum.golang.org',
    verifyChecksums: o.verify_checksums ?? true,
    nosumdb: o.nosumdb ?? [],
    distribution: o.distribution ?? '',
    flat: o.flat ?? false,
    metadataRegex: o.metadata_path_regex ?? '',
    contentDisposition: o.content_disposition ?? 'attachment',
    versionPolicy: o.version_policy ?? 'release',
    layoutPolicy: o.layout_policy ?? 'strict',
    repoPath: o.repo_path ?? '',
    gpgKey: o.gpg_key ?? null,
  }
}

function formatOptions(m: RepoFormModel): RepositoryWrite['format_options'] {
  switch (m.format) {
    case 'docker':
      return m.type !== 'proxy' ? { aliases: [], docker_hub: false, default_repo: m.defaultRepo } : { aliases: m.aliases, docker_hub: m.dockerHub, default_repo: m.defaultRepo }
    case 'npm':
      return { audit_passthrough: m.auditPassthrough }
    case 'apt':
      return { distribution: m.distribution.trim(), flat: m.flat }
    case 'pypi':
      return { json_api: m.jsonApi }
    case 'go':
      return {
        sumdb_proxy: m.sumdbProxy,
        sumdb_name: m.sumdbName.trim() || 'sum.golang.org',
        sumdb_url: m.sumdbUrl.trim() || 'https://sum.golang.org',
        verify_checksums: m.verifyChecksums,
        nosumdb: m.nosumdb.map((p) => p.trim()).filter(Boolean),
      }
    case 'yum':
      return { repo_path: m.repoPath, gpg_key: m.gpgKey }
    case 'helm':
      return {}
    case 'maven':
      return { version_policy: m.versionPolicy, layout_policy: m.layoutPolicy }
    case 'raw':
    default:
      // Mutable-path revalidation is a proxy setting (the server refuses it on hosted and group repositories).
      return { metadata_path_regex: m.type === 'proxy' ? m.metadataRegex.trim() || null : null, content_disposition: m.contentDisposition }
  }
}

/** The quota as written: none for groups (they store nothing) or when the limit is off. */
export function storageOf(m: RepoFormModel): NonNullable<RepositoryWrite['storage']> {
  const on = m.type !== 'group' && m.quotaEnabled && Number.isFinite(m.quotaSize) && m.quotaSize > 0
  return { quota_bytes: on ? toBytes(m.quotaSize, m.quotaUnit) : null, quota_mode: m.quotaMode }
}

export function toWrite(m: RepoFormModel): RepositoryWrite {
  const common = {
    name: m.name.trim(),
    format: m.format as Format,
    online: m.online,
    description: m.description.trim(),
    storage: storageOf(m),
    format_options: formatOptions(m),
  }
  if (m.type === 'hosted') {
    const patterns = m.immutableTags.map((p) => p.trim()).filter(Boolean)
    return { ...common, type: 'hosted', hosted: { write_policy: m.writePolicy, immutable_tag_patterns: patterns } }
  }
  if (m.type === 'group') {
    const writable = m.writableMember && m.members.includes(m.writableMember) ? m.writableMember : null
    return { ...common, type: 'group', group: { members: [...m.members], writable_member: writable } }
  }
  return {
    ...common,
    type: 'proxy',
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
      ca_pem: m.caPem.trim() || null,
      http_proxy: m.httpProxy.trim() || null,
      auto_block: {
        enabled: m.autoBlock,
        failures: Math.max(1, Math.round(m.autoBlockFailures)),
        initial_s: Math.max(1, Math.round(m.autoBlockInitialS)),
        max_s: Math.max(1, Math.round(m.autoBlockMaxMin * 60)),
      },
    },
  }
}

/** Client-side checks mirroring the server's (the server stays authoritative). */
export const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$/
