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

  it('validates names like the server', () => {
    expect(NAME_PATTERN.test('docker-hub')).toBe(true)
    expect(NAME_PATTERN.test('-bad')).toBe(false)
    expect(NAME_PATTERN.test('a')).toBe(false)
  })
})
