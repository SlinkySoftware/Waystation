// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository } from '@/api/types'

import { terraformProviderList, terraformVersionsFor } from './fixtures/terraform'
import { hostedProviderList, hostedProviderVersionsFor, terraformModuleList, terraformModuleVersionsFor } from './fixtures/terraformModules'
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

  it('sets up the network mirror and the module proxy for terraform and tofu', () => {
    const guide = setupGuide({ name: 'terraform-mirror', format: 'terraform', type: 'proxy', online: true, proxy: { upstream_url: 'https://registry.terraform.io' } } as Repository)
    expect(guide.clients.map((c) => c.id)).toEqual(['terraform', 'tofu'])
    const config = guide.clients[0]!.snippets[0]!.content
    expect(config).toContain('network_mirror')
    expect(config).toContain('/repository/terraform-mirror/')
    expect(config).toContain('host "registry.terraform.io"')
    expect(config).toContain('/repository/terraform-mirror/v1/modules/')
  })

  it('sets up the hosted repository: credentials, sources and publishing', () => {
    const guide = setupGuide({ name: 'terraform', format: 'terraform', type: 'hosted', online: true } as Repository)
    const ids = guide.clients[0]!.snippets.map((s) => s.id)
    expect(ids).toEqual(expect.arrayContaining(['terraform-cli-config', 'terraform-sources', 'terraform-publish-module', 'terraform-publish-provider']))
    expect(guide.clients[0]!.snippets.find((s) => s.id === 'terraform-publish-module')!.content).toContain('/repository/terraform/v1/modules/my-namespace/network/aws/1.0.0')
    expect(guide.clients[1]!.snippets.map((s) => s.id)).toContain('terraform-sources')
  })
})

describe('mock Terraform modules', () => {
  it('lists proxied and hosted modules', () => {
    expect(terraformModuleList(false).map((m) => m.module)).toContain('hashicorp/subnets/cidr')
    const hosted = terraformModuleList(true)
    expect(hosted.map((m) => m.module)).toContain('platform/network/aws')
    expect(hosted.every((m) => m.version_count === m.cached_versions)).toBe(true)
  })

  it('shows module versions with the archive held', () => {
    const vpc = terraformModuleVersionsFor('terraform-mirror', 'terraform-aws-modules/vpc/aws', false)!
    expect(vpc.versions.map((v) => v.version)).toEqual(['6.5.0', '6.4.0', '6.0.1', '5.21.0'])
    expect(vpc.versions[0]!.archive).toMatchObject({ filename: 'archive.tar.gz', path: 'v1/modules/terraform-aws-modules/vpc/aws/6.5.0/archive.tar.gz' })
    expect(vpc.versions[0]!.archive!.source).toMatch(/^git::https:\/\/github\.com\/terraform-aws-modules\/terraform-aws-vpc\?ref=/)
    expect(vpc.versions[3]!.archive).toBeNull()
    const hosted = terraformModuleVersionsFor('terraform', 'platform/network/aws', true)!
    expect(hosted.versions.every((v) => v.archive?.uploaded_by === 'admin')).toBe(true)
    expect(terraformModuleVersionsFor('terraform', 'nope/nope/aws', true)).toBeNull()
  })

  it('shows the hosted provider releases with their signing key', () => {
    expect(hostedProviderList()[0]!.provider).toBe('platform/inventory')
    const found = hostedProviderVersionsFor('platform/inventory')!
    expect(found.versions[0]).toMatchObject({ version: '1.2.0', key_id: '7C1A3B5D9E2F4A60', uploaded_by: 'admin' })
    expect(hostedProviderVersionsFor('registry.terraform.io/hashicorp/null')).toBeNull()
  })
})
