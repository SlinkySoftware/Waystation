// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository } from '@/api/types'

import { terraformProviderList, terraformVersionsFor } from './fixtures/terraform'
import { setupGuide } from './setup'

describe('mock Terraform providers', () => {
  it('lists providers of both registries by address', () => {
    const providers = terraformProviderList().map((p) => p.provider)
    expect(providers).toContain('registry.terraform.io/hashicorp/null')
    expect(providers).toContain('registry.opentofu.org/hashicorp/null')
    expect([...providers].sort((a, b) => a.localeCompare(b))).toEqual(providers)
    expect(terraformProviderList().every((p) => p.cached_archives >= 1)).toBe(true)
  })

  it('shows versions with platforms and cached archives', () => {
    const proxmox = terraformVersionsFor('terraform-mirror', 'registry.terraform.io/bpg/proxmox')!
    expect(proxmox.provider.latest).toBe('0.86.0')
    expect(proxmox.versions[3]).toMatchObject({ version: '0.84.1-rc1', prerelease: true, archives: [] })
    const archive = proxmox.versions[0]!.archives[0]!
    expect(archive).toMatchObject({ os: 'linux', arch: 'amd64', filename: 'terraform-provider-proxmox_0.86.0_linux_amd64.zip' })
    expect(archive.h1).toMatch(/^h1:/)
    expect(terraformVersionsFor('terraform-mirror', 'registry.example.com/acme/none')).toBeNull()
  })

  it('sets up the network mirror for terraform and tofu', () => {
    const guide = setupGuide({ name: 'terraform-mirror', format: 'terraform', type: 'proxy', online: true } as Repository)
    expect(guide.clients.map((c) => c.id)).toEqual(['terraform', 'tofu'])
    expect(guide.clients[0]!.snippets[0]!.content).toContain('network_mirror')
    expect(guide.clients[0]!.snippets[0]!.content).toContain('/repository/terraform-mirror/')
  })
})
