// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository } from '@/api/types'

import { emptyModel, modelFromRepository, NAME_PATTERN, PRESETS, toWrite } from './form'

describe('repository form model', () => {
  it('builds a docker proxy from a preset', () => {
    const m = { ...emptyModel('docker'), ...PRESETS.docker[0]!.apply }
    const w = toWrite(m)
    expect(w).toMatchObject({ name: 'docker-hub', format: 'docker', type: 'proxy', proxy: { upstream_url: 'https://registry-1.docker.io', metadata_max_age_s: 1800 } })
    expect(w.format_options).toEqual({ aliases: ['docker.io'], docker_hub: true, default_repo: false })
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
    expect(toWrite(m).format_options).toEqual({ distribution: 'trixie', flat: false })
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
    expect(w.format_options).toEqual({ repo_path: '10/extras/$basearch/os/', gpg_key: null })
    expect(toWrite(modelFromRepository({ ...repo, format_options: undefined } as unknown as Repository)).format_options).toEqual({ repo_path: '', gpg_key: null })
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
      format_options: { aliases: [], docker_hub: false, default_repo: false },
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
      format_options: { aliases: [], docker_hub: false, default_repo: false },
    })
    expect(toWrite({ ...m, writableMember: 'gone' }).group?.writable_member).toBeNull()
    const back = modelFromRepository({ ...toWrite(m), status: 'ok' } as unknown as Repository)
    expect(back).toMatchObject({ type: 'group', members: ['docker-hosted', 'docker-hub'], writableMember: 'docker-hosted' })
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
})
