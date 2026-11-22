// SPDX-License-Identifier: GPL-3.0-or-later
// Repository form model: a flat, form-friendly shape that maps to and from the API's RepositoryWrite.
import type { AptOptions, DockerOptions, Format, GalaxyOptions, GoOptions, M0Format, MavenOptions, NpmOptions, PypiOptions, QuotaMode, RawOptions, Repository, RepositoryWrite, SwiftOptions, TerraformOptions, WritePolicy, YumOptions } from '@/api/types'
import { t } from '@/i18n'
import { fromBytes, type QuotaUnit, toBytes } from '@/utils/quota'

/** Formats the form can hold: the creatable formats. */
export type FormFormat = M0Format

/** Repository types the form can create or edit: proxies for every format; hosted and group where the format serves them. */
export type FormType = 'proxy' | 'hosted' | 'group'

/** Formats whose hosted repositories are served (the create flow enables the "Hosted" tile for these). */
export const HOSTED_FORMATS: readonly FormFormat[] = ['docker', 'npm', 'raw', 'maven', 'pypi', 'helm', 'galaxy', 'yum', 'apt', 'terraform', 'swift']
/** Formats whose group repositories are served (the create flow enables the "Group" tile for these). */
export const GROUP_FORMATS: readonly FormFormat[] = ['docker', 'npm', 'raw', 'maven', 'pypi', 'go', 'helm', 'galaxy']
/** Formats whose groups may name a hosted writable member (uploads sent to the group land there). */
export const WRITABLE_GROUP_FORMATS: readonly FormFormat[] = ['docker', 'npm', 'raw', 'pypi', 'helm', 'galaxy']

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
  /** Dedicated port connector (<host>:<port>/<image>, no repository prefix); null = none. */
  connectorPort: number | null
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
  // galaxy (proxy)
  galaxyCollectionsPath: string
  galaxyRoles: boolean
  // terraform (proxy: provider network mirror and module registry proxy)
  tfOrigins: string[]
  // swift (SCM-backed proxy): how the Git host serves tag tarballs, scope → owner lines, allowlist mode
  swiftLayout: NonNullable<SwiftOptions['archive_layout']>
  swiftScopes: string
  swiftMappedOnly: boolean
  verifySignatures: boolean
  tfArchiveHosts: string[]
  // apt
  distribution: string
  flat: boolean
  /** Hosted: components uploads may go to (the first is the default). */
  aptComponents: string[]
  /** Hosted: architectures always published (packages' own architectures are added). */
  aptArchitectures: string[]
  // raw
  metadataRegex: string
  contentDisposition: 'inline' | 'attachment'
  // maven
  versionPolicy: NonNullable<MavenOptions['version_policy']>
  layoutPolicy: NonNullable<MavenOptions['layout_policy']>
  // yum (proxy)
  repoPath: string
  gpgKey: string | null
  /** Armored public keys repomd.xml must be signed with; empty = not verified. */
  repomdKeys: string
  // prefetch (proxy, PREFETCH_FORMATS): popular-metadata refresh, the warm list (one item per line), its schedule
  refreshPopular: boolean
  warmText: string
  /** Empty = the warm list runs only by hand. */
  prefetchSchedule: string
}

/** Hosted apt defaults (the server's defaults too). */
export const DEFAULT_APT_COMPONENTS: readonly string[] = ['main']
export const DEFAULT_APT_ARCHITECTURES: readonly string[] = ['amd64', 'arm64']

/** Provider registries a Terraform mirror serves by default (the server's default too). */
export const DEFAULT_TF_ORIGINS: readonly string[] = ['registry.terraform.io', 'registry.opentofu.org']
/** Hosts a Terraform module proxy caches module archives from by default (the server's default too). */
export const DEFAULT_TF_ARCHIVE_HOSTS: readonly string[] = ['github.com']

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
  galaxy: [{ id: 'galaxy', label: 'galaxy.ansible.com', apply: { name: 'galaxy-proxy', upstreamUrl: 'https://galaxy.ansible.com/' } }],
  terraform: [
    { id: 'registries', label: 'Terraform and OpenTofu registries', apply: { name: 'terraform-mirror', upstreamUrl: 'https://registry.terraform.io', tfOrigins: [...DEFAULT_TF_ORIGINS] } },
  ],
  swift: [
    { id: 'github', label: 'GitHub', apply: { name: 'swift-github', upstreamUrl: 'https://github.com/', swiftLayout: 'github' } },
    { id: 'gitlab', label: 'GitLab.com', apply: { name: 'swift-gitlab', upstreamUrl: 'https://gitlab.com/', swiftLayout: 'gitlab' } },
  ],
  yum: [
    { id: 'rocky', label: 'Rocky Linux', apply: { name: 'rocky', upstreamUrl: 'https://dl.rockylinux.org/pub/rocky/' } },
    { id: 'alma', label: 'AlmaLinux', apply: { name: 'almalinux', upstreamUrl: 'https://repo.almalinux.org/almalinux/' } },
    { id: 'oracle', label: 'Oracle Linux', apply: { name: 'oracle', upstreamUrl: 'https://yum.oracle.com/repo/OracleLinux/' } },
    { id: 'epel', label: 'EPEL', apply: { name: 'epel', upstreamUrl: 'https://dl.fedoraproject.org/pub/epel/' } },
  ],
}

/** Galaxy NG / galaxy.ansible.com layout of collection documents under the upstream URL (the server default). */
export const GALAXY_COLLECTIONS_PATH = 'api/v3/plugin/ansible/content/published/collections/index/'

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
    connectorPort: null,
    auditPassthrough: true,
    jsonApi: true,
    sumdbProxy: true,
    sumdbName: 'sum.golang.org',
    sumdbUrl: 'https://sum.golang.org',
    verifyChecksums: true,
    nosumdb: [],
    galaxyCollectionsPath: GALAXY_COLLECTIONS_PATH,
    galaxyRoles: true,
    tfOrigins: [...DEFAULT_TF_ORIGINS],
    verifySignatures: true,
    swiftLayout: 'github',
    swiftScopes: '',
    swiftMappedOnly: false,
    tfArchiveHosts: [...DEFAULT_TF_ARCHIVE_HOSTS],
    distribution: '',
    flat: false,
    aptComponents: [...DEFAULT_APT_COMPONENTS],
    aptArchitectures: [...DEFAULT_APT_ARCHITECTURES],
    metadataRegex: '',
    contentDisposition: 'attachment',
    versionPolicy: 'release',
    layoutPolicy: 'strict',
    repoPath: '',
    gpgKey: null,
    repomdKeys: '',
    refreshPopular: false,
    warmText: '',
    prefetchSchedule: '',
  }
}

export function modelFromRepository(repo: Repository): RepoFormModel {
  const m = emptyModel(repo.format as FormFormat, repo.type)
  const p = repo.proxy
  const h = repo.hosted
  const quotaBytes = repo.storage?.quota_bytes ?? null
  const quotaSize = quotaBytes ? fromBytes(quotaBytes) : { size: m.quotaSize, unit: m.quotaUnit }
  const o = (repo.format_options ?? {}) as DockerOptions & NpmOptions & Partial<AptOptions> & RawOptions & YumOptions & MavenOptions & PypiOptions & GoOptions & GalaxyOptions & TerraformOptions & SwiftOptions
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
    connectorPort: o.connector_port ?? null,
    auditPassthrough: o.audit_passthrough ?? true,
    jsonApi: o.json_api ?? true,
    sumdbProxy: o.sumdb_proxy ?? true,
    sumdbName: o.sumdb_name ?? 'sum.golang.org',
    sumdbUrl: o.sumdb_url ?? 'https://sum.golang.org',
    verifyChecksums: o.verify_checksums ?? true,
    nosumdb: o.nosumdb ?? [],
    galaxyCollectionsPath: o.collections_path ?? GALAXY_COLLECTIONS_PATH,
    galaxyRoles: o.roles ?? true,
    tfOrigins: o.origins ?? [...DEFAULT_TF_ORIGINS],
    verifySignatures: o.verify_signatures ?? true,
    swiftLayout: o.archive_layout ?? 'github',
    swiftScopes: Object.entries(o.scopes ?? {}).map(([scope, owner]) => `${scope} = ${owner}`).join('\n'),
    swiftMappedOnly: o.mapped_scopes_only ?? false,
    tfArchiveHosts: o.module_archive_hosts ?? [...DEFAULT_TF_ARCHIVE_HOSTS],
    distribution: o.distribution ?? '',
    flat: o.flat ?? false,
    aptComponents: o.components ?? [...DEFAULT_APT_COMPONENTS],
    aptArchitectures: o.architectures ?? [...DEFAULT_APT_ARCHITECTURES],
    metadataRegex: o.metadata_path_regex ?? '',
    contentDisposition: o.content_disposition ?? 'attachment',
    versionPolicy: o.version_policy ?? 'release',
    layoutPolicy: o.layout_policy ?? 'strict',
    repoPath: o.repo_path ?? '',
    gpgKey: o.gpg_key ?? null,
    repomdKeys: o.repomd_keys ?? '',
    refreshPopular: p?.prefetch?.refresh_popular ?? false,
    warmText: (p?.prefetch?.warm ?? []).join('\n'),
    prefetchSchedule: p?.prefetch?.schedule ?? '',
  }
}

function formatOptions(m: RepoFormModel): RepositoryWrite['format_options'] {
  switch (m.format) {
    case 'docker':
      return m.type !== 'proxy'
        ? { aliases: [], docker_hub: false, default_repo: m.defaultRepo, connector_port: m.connectorPort }
        : { aliases: m.aliases, docker_hub: m.dockerHub, default_repo: m.defaultRepo, connector_port: m.connectorPort }
    case 'npm':
      return { audit_passthrough: m.auditPassthrough }
    case 'apt': {
      // Components and architectures are hosted settings; a proxy carries what it has (the defaults).
      const components = m.aptComponents.map((c) => c.trim()).filter(Boolean)
      const architectures = m.aptArchitectures.map((a) => a.trim()).filter(Boolean)
      return {
        distribution: m.distribution.trim(),
        flat: m.type === 'hosted' ? false : m.flat,
        components: components.length ? components : [...DEFAULT_APT_COMPONENTS],
        architectures: architectures.length ? architectures : [...DEFAULT_APT_ARCHITECTURES],
      }
    }
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
    case 'terraform': {
      // Origins, signatures and archive hosts are proxy settings; the hosted repository keeps the defaults.
      if (m.type !== 'proxy') return {}
      const hosts = (list: string[]) => list.map((h) => h.trim().toLowerCase()).filter(Boolean)
      return { origins: hosts(m.tfOrigins), verify_signatures: m.verifySignatures, module_archive_hosts: hosts(m.tfArchiveHosts) }
    }
    case 'yum':
      return m.type === 'proxy' ? { repo_path: m.repoPath.trim(), gpg_key: m.gpgKey?.trim() || null, repomd_keys: m.repomdKeys.trim() || null } : {}
    case 'helm':
      return {}
    case 'swift':
      // The Git host settings are proxy settings (the server refuses them on hosted repositories).
      return m.type === 'proxy' ? { archive_layout: m.swiftLayout, scopes: parseScopes(m.swiftScopes), mapped_scopes_only: m.swiftMappedOnly } : {}
    case 'galaxy':
      // The upstream layout is a proxy setting (the server refuses another value on hosted and group repositories).
      return m.type === 'proxy' ? { collections_path: m.galaxyCollectionsPath.trim() || GALAXY_COLLECTIONS_PATH, roles: m.galaxyRoles } : {}
    case 'maven':
      return { version_policy: m.versionPolicy, layout_policy: m.layoutPolicy }
    case 'raw':
    default:
      // Mutable-path revalidation is a proxy setting (the server refuses it on hosted and group repositories).
      return { metadata_path_regex: m.type === 'proxy' ? m.metadataRegex.trim() || null : null, content_disposition: m.contentDisposition }
  }
}

/** "scope = owner" lines (blank lines and lines without "=" are ignored) → the swift ``scopes`` option. */
export function parseScopes(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const [scope, ...rest] = line.split('=')
    const owner = rest.join('=').trim()
    if (scope?.trim() && owner) out[scope.trim()] = owner
  }
  return out
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
      // Prefetch is only sent for the formats that support it (the server refuses it elsewhere).
      ...(PREFETCH_FORMATS.includes(m.format) ? { prefetch: prefetchOf(m) } : {}),
    },
  }
}

// ---------------------------------------------------------------- prefetch (T-O12)

/** Formats whose proxies can warm a list of items and keep popular metadata fresh. */
export const PREFETCH_FORMATS: readonly FormFormat[] = ['docker', 'npm', 'raw', 'maven', 'go']
/** Server limits of the warm list (PrefetchConfig). */
export const WARM_MAX_ITEMS = 200
export const WARM_MAX_LENGTH = 512
/** The shortest schedule the server accepts for a warm list. */
export const PREFETCH_MIN_INTERVAL_MIN = 15

/** Textarea → warm items: one per line, trimmed; blank lines dropped, repeats kept once (first wins). */
export function parseWarm(text: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const line of text.split('\n')) {
    const item = line.trim()
    if (item && !seen.has(item)) {
      seen.add(item)
      out.push(item)
    }
  }
  return out
}

export function prefetchOf(m: RepoFormModel): NonNullable<NonNullable<RepositoryWrite['proxy']>['prefetch']> {
  return { refresh_popular: m.refreshPopular, warm: parseWarm(m.warmText), schedule: m.prefetchSchedule.trim().replace(/\s+/g, ' ') || null }
}

const EVERY = /^@every\s+(\d+)\s*([smh])$/i

/** Quick client-side check of the warm list and its schedule (the server validates for real), keyed like the server's field errors. */
export function prefetchErrors(m: RepoFormModel): Record<string, string> {
  const errors: Record<string, string> = {}
  const items = parseWarm(m.warmText)
  if (items.length > WARM_MAX_ITEMS) errors['proxy.prefetch.warm'] = t('prefetch.error.tooMany', { max: WARM_MAX_ITEMS, n: items.length })
  const long = items.findIndex((item) => item.length > WARM_MAX_LENGTH)
  if (long >= 0) errors[`proxy.prefetch.warm.${long}`] = t('prefetch.error.tooLong', { max: WARM_MAX_LENGTH })
  const schedule = m.prefetchSchedule.trim().replace(/\s+/g, ' ')
  if (schedule) {
    const every = EVERY.exec(schedule)
    if (every) {
      const minutes = (Number(every[1]) * { s: 1, m: 60, h: 3600 }[every[2]!.toLowerCase() as 's' | 'm' | 'h']) / 60
      if (minutes < PREFETCH_MIN_INTERVAL_MIN) errors['proxy.prefetch.schedule'] = t('prefetch.error.tooOften', { n: PREFETCH_MIN_INTERVAL_MIN })
    } else if (schedule.startsWith('@')) {
      if (!['@hourly', '@daily', '@midnight', '@weekly', '@monthly', '@yearly', '@annually'].includes(schedule.toLowerCase())) errors['proxy.prefetch.schedule'] = t('prefetch.error.badMacro')
    } else if (schedule.split(' ').length !== 5) errors['proxy.prefetch.schedule'] = t('prefetch.error.fiveFields')
  }
  return errors
}

// ---------------------------------------------------------------- docker dedicated port (T-P6-DOCKERC)

/** The connector ports the server accepts (unprivileged TCP ports). */
export const CONNECTOR_PORT_MIN = 1024
export const CONNECTOR_PORT_MAX = 65535

/** Input value → model: empty/blank → null (no connector), anything else a number (validated by connectorPortError). */
export function parseConnectorPort(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null
  const text = String(value).trim()
  return text === '' ? null : Number(text)
}

/** Client-side range check of the dedicated port; uniqueness and the base URL port are the server's call. */
export function connectorPortError(m: RepoFormModel): string {
  const port = m.connectorPort
  if (m.format !== 'docker' || port === null) return ''
  return Number.isInteger(port) && port >= CONNECTOR_PORT_MIN && port <= CONNECTOR_PORT_MAX
    ? ''
    : t('repoForm.connectorPortRange', { min: CONNECTOR_PORT_MIN, max: CONNECTOR_PORT_MAX })
}

/** Client-side checks mirroring the server's (the server stays authoritative). */
export const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$/
