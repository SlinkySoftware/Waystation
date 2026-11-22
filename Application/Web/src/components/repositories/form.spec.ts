// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository } from '@/api/types'

import { connectorPortError, emptyModel, GROUP_FORMATS, HOSTED_FORMATS, modelFromRepository, NAME_PATTERN, parseConnectorPort, parseWarm, PREFETCH_FORMATS, prefetchErrors, PRESETS, toWrite, WARM_MAX_ITEMS, WRITABLE_GROUP_FORMATS } from './form'

describe('repository form model', () => {
  it('builds a docker proxy from a preset', () => {
    const m = { ...emptyModel('docker'), ...PRESETS.docker[0]!.apply }
    const w = toWrite(m)
    expect(w).toMatchObject({ name: 'docker-hub', format: 'docker', type: 'proxy', proxy: { upstream_url: 'https://registry-1.docker.io', metadata_max_age_s: 1800 } })
    expect(w.format_options).toEqual({ aliases: ['docker.io'], docker_hub: true, default_repo: false, connector_port: null })
  })

  it('builds helm proxy, hosted and group repositories (no format options)', () => {
    expect(HOSTED_FORMATS).toContain('helm')
    expect(GROUP_FORMATS).toContain('helm')
    expect(WRITABLE_GROUP_FORMATS).toContain('helm')
    const proxy = toWrite({ ...emptyModel('helm'), ...PRESETS.helm[0]!.apply })
    expect(proxy).toMatchObject({ format: 'helm', type: 'proxy', proxy: { upstream_url: 'https://prometheus-community.github.io/helm-charts' } })
    expect(proxy.format_options).toEqual({})
    expect(toWrite({ ...emptyModel('helm', 'hosted'), name: 'charts' })).toMatchObject({ type: 'hosted', hosted: { write_policy: 'allow_redeploy' } })
    const group = toWrite({ ...emptyModel('helm', 'group'), name: 'all', members: ['charts'], writableMember: 'charts' })
    expect(group.group).toEqual({ members: ['charts'], writable_member: 'charts' })
  })

  it('builds composer proxy (dist hosts, source mode), hosted and group repositories', () => {
    expect(HOSTED_FORMATS).toContain('composer')
    expect(GROUP_FORMATS).toContain('composer')
    expect(WRITABLE_GROUP_FORMATS).not.toContain('composer')
    const proxy = toWrite({ ...emptyModel('composer'), ...PRESETS.composer[0]!.apply })
    expect(proxy).toMatchObject({ name: 'composer-proxy', format: 'composer', type: 'proxy', proxy: { upstream_url: 'https://repo.packagist.org' } })
    expect(proxy.format_options).toEqual({ dist_hosts: ['github.com', 'codeload.github.com', 'api.github.com', 'gitlab.com'], source_mode: 'keep' })
    expect(toWrite({ ...emptyModel('composer'), composerDistHosts: [' GitHub.com ', ''], composerSourceMode: 'strip' }).format_options).toEqual({ dist_hosts: ['github.com'], source_mode: 'strip' })
    expect(toWrite({ ...emptyModel('composer', 'hosted'), name: 'composer-hosted' }).format_options).toEqual({})
    const group = toWrite({ ...emptyModel('composer', 'group'), name: 'composer-group', members: ['composer-hosted', 'composer-proxy'] })
    expect(group.group).toEqual({ members: ['composer-hosted', 'composer-proxy'], writable_member: null })
    expect(group.format_options).toEqual({})
    const back = modelFromRepository({
      name: 'composer-proxy', format: 'composer', type: 'proxy', online: true, status: 'online', url: 'https://registry.example.com/repository/composer-proxy/',
      format_options: { dist_hosts: ['github.com'], source_mode: 'strip' },
    } as unknown as Repository)
    expect([back.composerDistHosts, back.composerSourceMode]).toEqual([['github.com'], 'strip'])
    const defaults = modelFromRepository({ name: 'composer-hosted', format: 'composer', type: 'hosted', online: true, status: 'online', url: 'https://registry.example.com/repository/composer-hosted/', format_options: {} } as unknown as Repository)
    expect([defaults.composerDistHosts.length, defaults.composerSourceMode]).toEqual([4, 'keep'])
  })

  it('builds rubygems proxy, hosted and group repositories without format options', () => {
    expect(HOSTED_FORMATS).toContain('rubygems')
    expect(GROUP_FORMATS).toContain('rubygems')
    expect(WRITABLE_GROUP_FORMATS).not.toContain('rubygems')
    const proxy = toWrite({ ...emptyModel('rubygems'), ...PRESETS.rubygems[0]!.apply })
    expect(proxy).toMatchObject({ name: 'rubygems-proxy', format: 'rubygems', type: 'proxy', proxy: { upstream_url: 'https://rubygems.org' } })
    expect(proxy.format_options).toEqual({})
    expect(toWrite({ ...emptyModel('rubygems', 'hosted'), name: 'rubygems-hosted' }).format_options).toEqual({})
    const group = toWrite({ ...emptyModel('rubygems', 'group'), name: 'rubygems-group', members: ['rubygems-hosted', 'rubygems-proxy'] })
    expect(group.group).toEqual({ members: ['rubygems-hosted', 'rubygems-proxy'], writable_member: null })
    expect(group.format_options).toEqual({})
  })

  it('builds nuget proxy (catalog verification), hosted (delete mode) and group repositories', () => {
    expect(HOSTED_FORMATS).toContain('nuget')
    expect(GROUP_FORMATS).toContain('nuget')
    expect(WRITABLE_GROUP_FORMATS).toContain('nuget')
    const proxy = toWrite({ ...emptyModel('nuget'), ...PRESETS.nuget[0]!.apply })
    expect(proxy).toMatchObject({ name: 'nuget-proxy', format: 'nuget', type: 'proxy', proxy: { upstream_url: 'https://api.nuget.org/v3/index.json' } })
    expect(proxy.format_options).toEqual({ verify_catalog: true, delete_mode: 'unlist' })
    expect(toWrite({ ...emptyModel('nuget'), nugetVerifyCatalog: false }).format_options).toEqual({ verify_catalog: false, delete_mode: 'unlist' })
    const hosted = toWrite({ ...emptyModel('nuget', 'hosted'), name: 'nuget-hosted', nugetDeleteMode: 'delete' })
    expect(hosted).toMatchObject({ type: 'hosted', hosted: { write_policy: 'allow_redeploy' } })
    expect(hosted.format_options).toEqual({ verify_catalog: true, delete_mode: 'delete' })
    expect(toWrite({ ...emptyModel('nuget', 'hosted'), name: 'nuget-hosted' }).format_options).toEqual({ verify_catalog: true, delete_mode: 'unlist' })
    const group = toWrite({ ...emptyModel('nuget', 'group'), name: 'nuget-group', members: ['nuget-hosted', 'nuget.org-proxy'], writableMember: 'nuget-hosted' })
    expect(group.group).toEqual({ members: ['nuget-hosted', 'nuget.org-proxy'], writable_member: 'nuget-hosted' })
    expect(group.format_options).toEqual({})
    const back = modelFromRepository({
      name: 'nuget-hosted', format: 'nuget', type: 'hosted', online: true, status: 'online', url: 'https://registry.example.com/repository/nuget-hosted/',
      format_options: { delete_mode: 'delete' },
    } as unknown as Repository)
    expect([back.nugetDeleteMode, back.nugetVerifyCatalog]).toEqual(['delete', true])
    const backProxy = modelFromRepository({
      name: 'nuget.org-proxy', format: 'nuget', type: 'proxy', online: true, status: 'online', url: 'https://registry.example.com/repository/nuget.org-proxy/',
      format_options: { verify_catalog: false },
    } as unknown as Repository)
    expect([backProxy.nugetVerifyCatalog, backProxy.nugetDeleteMode]).toEqual([false, 'unlist'])
  })

  it('builds galaxy proxy (upstream layout and roles), hosted and group repositories', () => {
    expect(HOSTED_FORMATS).toContain('galaxy')
    expect(GROUP_FORMATS).toContain('galaxy')
    expect(WRITABLE_GROUP_FORMATS).toContain('galaxy')
    const proxy = toWrite({ ...emptyModel('galaxy'), ...PRESETS.galaxy[0]!.apply, galaxyRoles: false })
    expect(proxy).toMatchObject({ format: 'galaxy', type: 'proxy', proxy: { upstream_url: 'https://galaxy.ansible.com/' } })
    expect(proxy.format_options).toEqual({ collections_path: 'api/v3/plugin/ansible/content/published/collections/index/', roles: false })
    const plain = toWrite({ ...emptyModel('galaxy'), galaxyCollectionsPath: ' api/v3/collections/ ' })
    expect(plain.format_options).toEqual({ collections_path: 'api/v3/collections/', roles: true })
    expect(toWrite({ ...emptyModel('galaxy', 'hosted'), name: 'collections' }).format_options).toEqual({})
    const group = toWrite({ ...emptyModel('galaxy', 'group'), name: 'all', members: ['collections'], writableMember: 'collections' })
    expect(group.group).toEqual({ members: ['collections'], writable_member: 'collections' })
    const back = modelFromRepository({
      name: 'gal', format: 'galaxy', type: 'proxy', online: true, status: 'online', url: 'https://registry.example.com/repository/gal/',
      format_options: { collections_path: 'api/v3/collections/', roles: false },
    } as unknown as Repository)
    expect([back.galaxyCollectionsPath, back.galaxyRoles]).toEqual(['api/v3/collections/', false])
  })

  it('builds swift SCM proxies (Git host layout, scope mapping) and hosted registries (no groups)', () => {
    expect(HOSTED_FORMATS).toContain('swift')
    expect(GROUP_FORMATS).not.toContain('swift')
    const proxy = toWrite({ ...emptyModel('swift'), ...PRESETS.swift[1]!.apply, swiftScopes: 'pointfree = pointfreeco\n\nbad line\n grp = group/sub ', swiftMappedOnly: true })
    expect(proxy).toMatchObject({ name: 'swift-gitlab', format: 'swift', type: 'proxy', proxy: { upstream_url: 'https://gitlab.com/' } })
    expect(proxy.format_options).toEqual({ archive_layout: 'gitlab', scopes: { pointfree: 'pointfreeco', grp: 'group/sub' }, mapped_scopes_only: true })
    expect(toWrite({ ...emptyModel('swift', 'hosted'), name: 'packages' }).format_options).toEqual({})
    const back = modelFromRepository({
      name: 'spm', format: 'swift', type: 'proxy', online: true, status: 'online', url: 'https://registry.example.com/repository/spm/',
      format_options: { archive_layout: 'gitea', scopes: { a: 'b' }, mapped_scopes_only: true },
    } as unknown as Repository)
    expect([back.swiftLayout, back.swiftScopes, back.swiftMappedOnly]).toEqual(['gitea', 'a = b', true])
  })

  it('builds npm hosted repositories and groups with a writable member', () => {
    expect(HOSTED_FORMATS).toContain('npm')
    expect(GROUP_FORMATS).toContain('npm')
    expect(WRITABLE_GROUP_FORMATS).toContain('npm')
    const hosted = toWrite({ ...emptyModel('npm', 'hosted'), name: 'npm-internal', writePolicy: 'disable_redeploy' })
    expect(hosted).toMatchObject({ format: 'npm', type: 'hosted', hosted: { write_policy: 'disable_redeploy' } })
    const group = toWrite({ ...emptyModel('npm', 'group'), name: 'npm-all', members: ['npm-internal', 'npm'], writableMember: 'npm-internal' })
    expect(group).toMatchObject({ type: 'group', group: { members: ['npm-internal', 'npm'], writable_member: 'npm-internal' } })
  })

  it('builds apt hosted repositories with a distribution, components and architectures', () => {
    expect(HOSTED_FORMATS).toContain('apt')
    const m = { ...emptyModel('apt', 'hosted'), name: 'debs', distribution: ' stable ', flat: true, aptComponents: ['main', ' contrib ', ''], aptArchitectures: [] }
    expect(toWrite(m)).toMatchObject({
      type: 'hosted',
      hosted: { write_policy: 'allow_redeploy' },
      format_options: { distribution: 'stable', flat: false, components: ['main', 'contrib'], architectures: ['amd64', 'arm64'] },
    })
    const back = modelFromRepository({ name: 'debs', format: 'apt', type: 'hosted', online: true, format_options: { distribution: 'stable', components: ['main', 'non-free'], architectures: ['armhf'] } } as unknown as Repository)
    expect(back).toMatchObject({ distribution: 'stable', aptComponents: ['main', 'non-free'], aptArchitectures: ['armhf'] })
    expect(toWrite({ ...emptyModel('apt', 'proxy'), name: 'debian', distribution: 'trixie' }).format_options).toEqual({ distribution: 'trixie', flat: false, components: ['main'], architectures: ['amd64', 'arm64'] })
  })

  it('never sends an empty secret (write-only: omitted keeps the stored one)', () => {
    const m = { ...emptyModel('npm'), name: 'npm-proxy', upstreamUrl: 'https://registry.npmjs.org', authKind: 'bearer' as const }
    expect(toWrite(m).proxy?.auth).toEqual({ kind: 'bearer', username: null })
    expect(toWrite({ ...m, secret: 's3cret' }).proxy?.auth).toEqual({ kind: 'bearer', username: null, secret: 's3cret' })
  })

  it('round-trips an existing repository', () => {
    const repo = {
      name: 'debian',
      format: 'apt',
      type: 'proxy',
      online: true,
      description: 'Debian',
      proxy: { upstream_url: 'https://deb.debian.org/debian', auth: { kind: 'none', secret_set: false }, metadata_max_age_s: 600, negative_ttl_s: 600, stale_on_error: true, offline: false, allow_private_upstream: false },
      format_options: { distribution: 'trixie', flat: false },
    } as unknown as Repository
    const m = modelFromRepository(repo)
    expect(m.metadataMaxAgeMin).toBe(10)
    expect(toWrite(m).format_options).toEqual({ distribution: 'trixie', flat: false, components: ['main'], architectures: ['amd64', 'arm64'] })
  })

  it('edits a yum repository keeping its format options and content max age', () => {
    const repo = {
      name: 'rocky',
      format: 'yum',
      type: 'proxy',
      online: true,
      description: 'Rocky Linux',
      proxy: { upstream_url: 'https://dl.rockylinux.org/pub/rocky/', auth: { kind: 'none', secret_set: false }, metadata_max_age_s: 3600, content_max_age_s: 86400, negative_ttl_s: 600, stale_on_error: true, offline: false, allow_private_upstream: false },
      format_options: { repo_path: '10/extras/$basearch/os/', gpg_key: null },
    } as unknown as Repository
    const w = toWrite(modelFromRepository(repo))
    expect(w.format).toBe('yum')
    expect(w.format_options).toEqual({ repo_path: '10/extras/$basearch/os/', gpg_key: null, repomd_keys: null })
    expect(toWrite(modelFromRepository({ ...repo, format_options: undefined } as unknown as Repository)).format_options).toEqual({ repo_path: '', gpg_key: null, repomd_keys: null })
    expect(toWrite({ ...modelFromRepository(repo), repomdKeys: ' KEY ' }).format_options).toMatchObject({ repomd_keys: 'KEY' })
    expect(w.proxy?.content_max_age_s).toBe(86400)
    expect(w.proxy?.metadata_max_age_s).toBe(3600)
  })

  it('builds a hosted docker repository (no upstream, write policy and immutable tags)', () => {
    const m = { ...emptyModel('docker', 'hosted'), name: 'docker-hosted', immutableTags: [' v[0-9]+ ', ''], writePolicy: 'disable_redeploy' as const, aliases: ['ignored'] }
    const w = toWrite(m)
    expect(w).toEqual({
      name: 'docker-hosted',
      format: 'docker',
      type: 'hosted',
      online: true,
      description: '',
      hosted: { write_policy: 'disable_redeploy', immutable_tag_patterns: ['v[0-9]+'] },
      storage: { quota_bytes: null, quota_mode: 'soft' },
      format_options: { aliases: [], docker_hub: false, default_repo: false, connector_port: null },
    })
    expect(w.proxy).toBeUndefined()
    const back = modelFromRepository({ ...w, status: 'ok', url: '', created_at: '', updated_at: '', stats: {} } as unknown as Repository)
    expect(back.type).toBe('hosted')
    expect(back.writePolicy).toBe('disable_redeploy')
    expect(back.immutableTags).toEqual(['v[0-9]+'])
  })

  it('builds a docker group (members in order, writable member only when it is a member)', () => {
    const m = { ...emptyModel('docker', 'group'), name: 'docker-all', members: ['docker-hosted', 'docker-hub'], writableMember: 'docker-hosted' }
    expect(toWrite(m)).toEqual({
      name: 'docker-all',
      format: 'docker',
      type: 'group',
      online: true,
      description: '',
      group: { members: ['docker-hosted', 'docker-hub'], writable_member: 'docker-hosted' },
      storage: { quota_bytes: null, quota_mode: 'soft' },
      format_options: { aliases: [], docker_hub: false, default_repo: false, connector_port: null },
    })
    expect(toWrite({ ...m, writableMember: 'gone' }).group?.writable_member).toBeNull()
    const back = modelFromRepository({ ...toWrite(m), status: 'ok' } as unknown as Repository)
    expect(back).toMatchObject({ type: 'group', members: ['docker-hosted', 'docker-hub'], writableMember: 'docker-hosted' })
  })

  it('builds Maven proxy, hosted and group repositories with their policies', () => {
    const proxy = toWrite({ ...emptyModel('maven'), ...PRESETS.maven[0]!.apply })
    expect(proxy).toMatchObject({ name: 'maven-central', format: 'maven', type: 'proxy', proxy: { upstream_url: 'https://repo.maven.apache.org/maven2/' } })
    expect(proxy.format_options).toEqual({ version_policy: 'release', layout_policy: 'strict' })
    const hosted = toWrite({ ...emptyModel('maven', 'hosted'), name: 'maven-snapshots', versionPolicy: 'snapshot', layoutPolicy: 'permissive' })
    expect(hosted).toMatchObject({ type: 'hosted', format_options: { version_policy: 'snapshot', layout_policy: 'permissive' } })
    const group = toWrite({ ...emptyModel('maven', 'group'), name: 'maven-public', members: ['maven-releases', 'maven-central'] })
    expect(group).toMatchObject({ type: 'group', group: { members: ['maven-releases', 'maven-central'], writable_member: null } })
    const back = modelFromRepository({ name: 'maven-snapshots', format: 'maven', type: 'hosted', online: true, format_options: { version_policy: 'mixed' } } as unknown as Repository)
    expect([back.versionPolicy, back.layoutPolicy]).toEqual(['mixed', 'strict'])
  })

  it('maps the network and auto-block options', () => {
    const m = { ...emptyModel('raw'), name: 'files', upstreamUrl: 'https://files.example.com' }
    expect(toWrite(m).proxy).toMatchObject({ ca_pem: null, http_proxy: null, auto_block: { enabled: true, failures: 5, initial_s: 30, max_s: 1800 } })
    const set = { ...m, caPem: ' -----BEGIN CERTIFICATE-----\n... ', httpProxy: ' http://proxy.example.com:3128 ', autoBlock: false, autoBlockMaxMin: 60 }
    const w = toWrite(set)
    expect(w.proxy).toMatchObject({ ca_pem: '-----BEGIN CERTIFICATE-----\n...', http_proxy: 'http://proxy.example.com:3128', auto_block: { enabled: false, max_s: 3600 } })
    const back = modelFromRepository({ name: 'files', format: 'raw', type: 'proxy', online: true, proxy: w.proxy } as unknown as Repository)
    expect(back).toMatchObject({ caPem: set.caPem.trim(), httpProxy: 'http://proxy.example.com:3128', autoBlock: false, autoBlockMaxMin: 60 })
  })

  it('builds PyPI proxies, hosted repositories and groups with a writable member', () => {
    const proxy = toWrite({ ...emptyModel('pypi'), ...PRESETS.pypi[0]!.apply })
    expect(proxy).toMatchObject({ name: 'pypi-proxy', format: 'pypi', type: 'proxy', proxy: { upstream_url: 'https://pypi.org' }, format_options: { json_api: true } })
    const hosted = toWrite({ ...emptyModel('pypi', 'hosted'), name: 'pypi-internal', writePolicy: 'disable_redeploy' })
    expect(hosted).toMatchObject({ type: 'hosted', hosted: { write_policy: 'disable_redeploy' } })
    const group = toWrite({ ...emptyModel('pypi', 'group'), name: 'pypi-all', members: ['pypi-internal', 'pypi-proxy'], writableMember: 'pypi-internal' })
    expect(group).toMatchObject({ type: 'group', group: { members: ['pypi-internal', 'pypi-proxy'], writable_member: 'pypi-internal' } })
    const back = modelFromRepository({ ...proxy, format_options: { json_api: false }, status: 'ok' } as unknown as Repository)
    expect(back.jsonApi).toBe(false)
    expect(WRITABLE_GROUP_FORMATS).toContain('pypi')
  })

  it('builds Cargo proxies of the crates.io sparse index, hosted registries and groups with a writable member', () => {
    expect([HOSTED_FORMATS.includes('cargo'), GROUP_FORMATS.includes('cargo'), WRITABLE_GROUP_FORMATS.includes('cargo')]).toEqual([true, true, true])
    const proxy = toWrite({ ...emptyModel('cargo'), ...PRESETS.cargo[0]!.apply, warmText: 'serde@1.0.228\nitoa' })
    expect(proxy).toMatchObject({ name: 'cargo-proxy', format: 'cargo', type: 'proxy', proxy: { upstream_url: 'https://index.crates.io/', prefetch: { warm: ['serde@1.0.228', 'itoa'] } }, format_options: {} })
    expect(toWrite({ ...emptyModel('cargo', 'hosted'), name: 'cargo-hosted' })).toMatchObject({ type: 'hosted', format_options: {} })
    const group = toWrite({ ...emptyModel('cargo', 'group'), name: 'cargo-group', members: ['cargo-hosted', 'cargo-proxy'], writableMember: 'cargo-hosted' })
    expect(group).toMatchObject({ type: 'group', group: { members: ['cargo-hosted', 'cargo-proxy'], writable_member: 'cargo-hosted' } })
  })

  it('builds conda proxies of conda-forge and hosted channels, never groups', () => {
    expect([HOSTED_FORMATS.includes('conda'), GROUP_FORMATS.includes('conda')]).toEqual([true, false])
    const proxy = toWrite({ ...emptyModel('conda'), ...PRESETS.conda[0]!.apply, warmText: 'noarch/tzdata==2024b' })
    expect(proxy).toMatchObject({ name: 'conda-forge', format: 'conda', type: 'proxy', proxy: { upstream_url: 'https://conda.anaconda.org/conda-forge', prefetch: { warm: ['noarch/tzdata==2024b'] } }, format_options: {} })
    expect(toWrite({ ...emptyModel('conda', 'hosted'), name: 'conda-hosted' })).toMatchObject({ type: 'hosted', format_options: {} })
  })

  it('maps raw hosted and group repositories', () => {
    const hosted = { ...emptyModel('raw'), type: 'hosted' as const, name: 'files', metadataRegex: '^/x', writePolicy: 'disable_redeploy' as const }
    const w = toWrite(hosted)
    expect(w).toMatchObject({ type: 'hosted', hosted: { write_policy: 'disable_redeploy' } })
    expect(w.format_options).toEqual({ metadata_path_regex: null, content_disposition: 'attachment' })
    const group = { ...emptyModel('raw'), type: 'group' as const, name: 'all-files', members: ['files', 'mirror'], writableMember: 'files' }
    expect(toWrite(group).group).toEqual({ members: ['files', 'mirror'], writable_member: 'files' })
    expect(HOSTED_FORMATS).toContain('raw')
    expect(GROUP_FORMATS).toContain('raw')
    expect(WRITABLE_GROUP_FORMATS).toContain('raw')
  })

  it('round-trips the docker dedicated port for proxy and hosted (none by default)', () => {
    expect(emptyModel('docker').connectorPort).toBeNull()
    expect(modelFromRepository({ ...toWrite({ ...emptyModel('docker'), name: 'hub' }), status: 'ok' } as unknown as Repository).connectorPort).toBeNull()
    for (const type of ['proxy', 'hosted', 'group'] as const) {
      const m = { ...emptyModel('docker', type), name: `docker-${type}`, connectorPort: 5001 }
      const w = toWrite(m)
      expect((w.format_options as { connector_port?: number | null }).connector_port).toBe(5001)
      expect(modelFromRepository({ ...w, status: 'ok' } as unknown as Repository).connectorPort).toBe(5001)
    }
  })

  it('checks the dedicated port range on the client (blank is none)', () => {
    const m = emptyModel('docker', 'hosted')
    expect(parseConnectorPort('')).toBeNull()
    expect(parseConnectorPort(null)).toBeNull()
    expect(parseConnectorPort(' 5001 ')).toBe(5001)
    expect(parseConnectorPort(5002)).toBe(5002)
    expect(connectorPortError(m)).toBe('')
    expect(connectorPortError({ ...m, connectorPort: 1024 })).toBe('')
    expect(connectorPortError({ ...m, connectorPort: 65535 })).toBe('')
    expect(connectorPortError({ ...m, connectorPort: 443 })).toBe('A whole number from 1024 to 65535')
    expect(connectorPortError({ ...m, connectorPort: 65536 })).not.toBe('')
    expect(connectorPortError({ ...m, connectorPort: 5000.5 })).not.toBe('')
    expect(connectorPortError({ ...m, connectorPort: Number('abc') })).not.toBe('')
  })

  it('maps the storage quota (bytes from size and unit; none for groups or when off)', () => {
    const m = { ...emptyModel('docker', 'hosted'), name: 'docker-hosted' }
    expect(toWrite(m).storage).toEqual({ quota_bytes: null, quota_mode: 'soft' })
    const on = { ...m, quotaEnabled: true, quotaSize: 1.5, quotaUnit: 'GiB' as const, quotaMode: 'hard' as const }
    expect(toWrite(on).storage).toEqual({ quota_bytes: 1.5 * 1024 ** 3, quota_mode: 'hard' })
    expect(toWrite({ ...on, quotaSize: 0 }).storage?.quota_bytes).toBeNull()
    expect(toWrite({ ...on, type: 'group' }).storage?.quota_bytes).toBeNull()
    const back = modelFromRepository({ ...toWrite({ ...on, quotaSize: 512, quotaUnit: 'MiB' }), status: 'ok' } as unknown as Repository)
    expect(back).toMatchObject({ quotaEnabled: true, quotaSize: 512, quotaUnit: 'MiB', quotaMode: 'hard' })
    expect(modelFromRepository({ ...toWrite(m), storage: { quota_bytes: 2 * 1024 ** 4 } } as unknown as Repository)).toMatchObject({ quotaSize: 2, quotaUnit: 'TiB', quotaMode: 'soft' })
  })

  it('validates names like the server', () => {
    expect(NAME_PATTERN.test('docker-hub')).toBe(true)
    expect(NAME_PATTERN.test('-bad')).toBe(false)
    expect(NAME_PATTERN.test('a')).toBe(false)
  })

  it('maps Go proxy options and offers proxy and group only', () => {
    const proxy = toWrite({ ...emptyModel('go'), ...PRESETS.go[0]!.apply, nosumdb: [' example.com/private/* ', ''] })
    expect(proxy).toMatchObject({
      name: 'go-proxy',
      format: 'go',
      type: 'proxy',
      proxy: { upstream_url: 'https://proxy.golang.org' },
      format_options: { sumdb_proxy: true, sumdb_name: 'sum.golang.org', sumdb_url: 'https://sum.golang.org', verify_checksums: true, nosumdb: ['example.com/private/*'] },
    })
    const repo = { name: 'go-proxy', format: 'go', type: 'proxy', online: true, format_options: { sumdb_proxy: false, verify_checksums: false, nosumdb: ['corp.example.com'] } } as unknown as Repository
    expect(modelFromRepository(repo)).toMatchObject({ sumdbProxy: false, verifyChecksums: false, nosumdb: ['corp.example.com'], sumdbName: 'sum.golang.org' })
    expect(GROUP_FORMATS).toContain('go')
    expect(HOSTED_FORMATS).not.toContain('go')
    expect(WRITABLE_GROUP_FORMATS).not.toContain('go')
  })

  it('maps Terraform mirror options and offers proxy only', () => {
    const proxy = toWrite({ ...emptyModel('terraform'), ...PRESETS.terraform[0]!.apply })
    expect(proxy).toMatchObject({
      name: 'terraform-mirror',
      format: 'terraform',
      type: 'proxy',
      proxy: { upstream_url: 'https://registry.terraform.io' },
      format_options: { origins: ['registry.terraform.io', 'registry.opentofu.org'], verify_signatures: true, module_archive_hosts: ['github.com'] },
    })
    const custom = toWrite({ ...emptyModel('terraform'), tfOrigins: [' Registry.Example.com ', ''], verifySignatures: false, tfArchiveHosts: ['GitHub.com', ' downloads.example.com'] })
    expect(custom.format_options).toEqual({ origins: ['registry.example.com'], verify_signatures: false, module_archive_hosts: ['github.com', 'downloads.example.com'] })
    const repo = { name: 'tf', format: 'terraform', type: 'proxy', online: true, format_options: { origins: ['tf.example.com'], verify_signatures: false, module_archive_hosts: [] } } as unknown as Repository
    expect(modelFromRepository(repo)).toMatchObject({ tfOrigins: ['tf.example.com'], verifySignatures: false, tfArchiveHosts: [] })
    expect(modelFromRepository({ ...repo, format_options: {} } as unknown as Repository)).toMatchObject({ tfOrigins: ['registry.terraform.io', 'registry.opentofu.org'], verifySignatures: true, tfArchiveHosts: ['github.com'] })
    expect(GROUP_FORMATS).not.toContain('terraform')
    // the hosted repository (one per instance): a private module and provider registry, default options
    expect(HOSTED_FORMATS).toContain('terraform')
    expect(toWrite({ ...emptyModel('terraform', 'hosted'), name: 'terraform' })).toMatchObject({ type: 'hosted', format_options: {} })
  })

  it('parses the warm list: one item per line, trimmed, blank lines and repeats dropped', () => {
    expect(parseWarm('  library/nginx:1.27 \n\n\nlibrary/alpine:3.20\nlibrary/nginx:1.27\nnginx:1.27 linux/amd64,linux/arm64\n')).toEqual([
      'library/nginx:1.27',
      'library/alpine:3.20',
      'nginx:1.27 linux/amd64,linux/arm64',
    ])
    expect(parseWarm('')).toEqual([])
    expect(parseWarm(' \n \n')).toEqual([])
  })

  it('writes prefetch for the formats that support it and reads it back', () => {
    expect([...PREFETCH_FORMATS]).toEqual(['docker', 'npm', 'raw', 'maven', 'go', 'nuget', 'composer', 'cargo', 'conda', 'rubygems'])
    const m = { ...emptyModel('npm'), name: 'npm-proxy', upstreamUrl: 'https://registry.npmjs.org', refreshPopular: true, warmText: 'lodash@4.17.21\n\n @types/node@latest ', prefetchSchedule: '  0  3 * * * ' }
    const w = toWrite(m)
    expect(w.proxy?.prefetch).toEqual({ refresh_popular: true, warm: ['lodash@4.17.21', '@types/node@latest'], schedule: '0 3 * * *' })
    // Empty schedule: only by hand (null).
    expect(toWrite({ ...m, prefetchSchedule: ' ' }).proxy?.prefetch?.schedule).toBeNull()
    // Formats without prefetch never send it; hosted and group repositories have no proxy block at all.
    expect(toWrite({ ...emptyModel('apt'), refreshPopular: true }).proxy).not.toHaveProperty('prefetch')
    expect(toWrite({ ...emptyModel('npm', 'hosted'), name: 'npm-hosted' }).proxy).toBeUndefined()
    const back = modelFromRepository({
      name: 'hub', format: 'docker', type: 'proxy', online: true,
      proxy: { upstream_url: 'https://registry-1.docker.io', prefetch: { refresh_popular: true, warm: ['library/nginx:1.27', 'library/alpine:3.20'], schedule: '@daily' } },
    } as unknown as Repository)
    expect([back.refreshPopular, back.warmText, back.prefetchSchedule]).toEqual([true, 'library/nginx:1.27\nlibrary/alpine:3.20', '@daily'])
    const none = modelFromRepository({ name: 'hub', format: 'docker', type: 'proxy', online: true, proxy: { upstream_url: 'https://registry-1.docker.io' } } as unknown as Repository)
    expect([none.refreshPopular, none.warmText, none.prefetchSchedule]).toEqual([false, '', ''])
  })

  it('checks the warm list and its schedule on the client (keys like the server field errors)', () => {
    const m = emptyModel('docker')
    expect(prefetchErrors(m)).toEqual({})
    expect(prefetchErrors({ ...m, prefetchSchedule: '@daily' })).toEqual({})
    expect(prefetchErrors({ ...m, prefetchSchedule: '@every 15m' })).toEqual({})
    expect(prefetchErrors({ ...m, prefetchSchedule: '30 2 * * MON-FRI' })).toEqual({})
    expect(prefetchErrors({ ...m, prefetchSchedule: '@every 10m' })['proxy.prefetch.schedule']).toBe('Use at least @every 15m')
    expect(prefetchErrors({ ...m, prefetchSchedule: '@every 30s' })['proxy.prefetch.schedule']).toBe('Use at least @every 15m')
    expect(prefetchErrors({ ...m, prefetchSchedule: '@sometimes' })['proxy.prefetch.schedule']).toContain('@hourly')
    expect(prefetchErrors({ ...m, prefetchSchedule: '0 3 * *' })['proxy.prefetch.schedule']).toContain('5 fields')
    const many = Array.from({ length: WARM_MAX_ITEMS + 1 }, (_, i) => `library/image-${i}:1.0`).join('\n')
    expect(prefetchErrors({ ...m, warmText: many })['proxy.prefetch.warm']).toBe('At most 200 items (201 entered)')
    expect(prefetchErrors({ ...m, warmText: `library/nginx:1.27\n${'a'.repeat(513)}` })).toEqual({ 'proxy.prefetch.warm.1': 'An item is at most 512 characters' })
  })
})
