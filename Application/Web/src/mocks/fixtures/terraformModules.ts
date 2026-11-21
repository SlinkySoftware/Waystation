// SPDX-License-Identifier: GPL-3.0-or-later
// Terraform modules in mock mode: a module proxy holds real public modules (GitHub tag archives it cached); the hosted
// repository holds a team's own modules and one private provider release.
import type { TerraformModule, TerraformModuleVersion, TerraformProvider, TerraformVersion } from '@/api/types'

import { fakeSha256, hash, KiB, rng } from '../rng'

const NOW = Date.now()
const DAY = 86_400_000

interface ModuleSeed {
  module: string
  versions: string[] // newest first
  kib: number
  held: number // newest versions whose archive is held
  source?: string // proxy: the GitHub repository the registry names
}

const PROXIED: ModuleSeed[] = [
  { module: 'terraform-aws-modules/vpc/aws', versions: ['6.5.0', '6.4.0', '6.0.1', '5.21.0'], kib: 64, held: 2, source: 'github.com/terraform-aws-modules/terraform-aws-vpc' },
  { module: 'terraform-aws-modules/eks/aws', versions: ['21.8.0', '21.7.0', '20.37.2'], kib: 180, held: 1, source: 'github.com/terraform-aws-modules/terraform-aws-eks' },
  { module: 'hashicorp/subnets/cidr', versions: ['1.0.0', '0.1.0'], kib: 10, held: 1, source: 'github.com/hashicorp/terraform-cidr-subnets' },
  { module: 'hashicorp/consul/aws', versions: ['0.12.0', '0.11.0', '0.10.0-beta1'], kib: 120, held: 1, source: 'github.com/hashicorp/terraform-aws-consul' },
]

const HOSTED: ModuleSeed[] = [
  { module: 'platform/network/aws', versions: ['2.3.0', '2.2.1', '2.2.0', '1.9.4'], kib: 18, held: 4 },
  { module: 'platform/cluster/proxmox', versions: ['0.4.0', '0.3.2'], kib: 26, held: 2 },
  { module: 'platform/dns/cloudflare', versions: ['1.1.0'], kib: 6, held: 1 },
]

const PRERELEASE = /-/

function seeds(hosted: boolean): ModuleSeed[] {
  return hosted ? HOSTED : PROXIED
}

function size(seed: ModuleSeed, version: string): number {
  const next = rng(hash(`tfmod:${seed.module}@${version}`))
  return Math.round(seed.kib * KiB * (0.85 + next() * 0.3))
}

function summary(seed: ModuleSeed, hosted: boolean): TerraformModule {
  const held = seed.versions.slice(0, seed.held)
  return {
    module: seed.module,
    latest: seed.versions.find((v) => !PRERELEASE.test(v)) ?? seed.versions[0] ?? null,
    version_count: hosted ? held.length : seed.versions.length,
    cached_versions: held.length,
    size_bytes: held.reduce((sum, v) => sum + size(seed, v), 0),
  }
}

export function terraformModuleList(hosted: boolean): TerraformModule[] {
  return [...seeds(hosted)].sort((a, b) => a.module.localeCompare(b.module)).map((s) => summary(s, hosted))
}

export function terraformModuleVersionsFor(repo: string, module: string, hosted: boolean): { module: TerraformModule; versions: TerraformModuleVersion[] } | null {
  const seed = seeds(hosted).find((m) => m.module === module.trim())
  if (!seed) return null
  const next = rng(hash(`tfmodver:${repo}:${seed.module}`))
  const listed = hosted ? seed.versions.slice(0, seed.held) : seed.versions
  const versions = listed.map((version, i): TerraformModuleVersion => ({
    version,
    prerelease: PRERELEASE.test(version),
    listed: true,
    archive:
      i < seed.held
        ? {
            filename: 'archive.tar.gz',
            path: `v1/modules/${seed.module}/${version}/archive.tar.gz`,
            size: size(seed, version),
            sha256: fakeSha256(`${repo}/${seed.module}/${version}`),
            source: seed.source ? `git::https://${seed.source}?ref=${fakeSha256(`${seed.source}@${version}`).slice(0, 40)}` : null,
            uploaded_by: hosted ? 'admin' : null,
            created_at: new Date(NOW - (i + 1) * next() * 20 * DAY).toISOString(),
            last_downloaded_at: new Date(NOW - next() * 3 * DAY).toISOString(),
            downloads: Math.round(next() * 120),
          }
        : null,
  }))
  return { module: summary(seed, hosted), versions }
}

// ---------------------------------------------------------------------------- the hosted repository's providers
const KEY_ID = '7C1A3B5D9E2F4A60'

export function hostedProviderList(): TerraformProvider[] {
  return [{ provider: 'platform/inventory', latest: '1.2.0', version_count: 2, cached_archives: 4, size_bytes: 4 * 9_600 * KiB }]
}

export function hostedProviderVersionsFor(provider: string): { provider: TerraformProvider; versions: TerraformVersion[] } | null {
  if (provider.trim() !== 'platform/inventory') return null
  const versions = ['1.2.0', '1.1.0'].map((version, i): TerraformVersion => {
    const archives = ['darwin_arm64', 'linux_amd64'].map((platform) => {
      const [os = 'linux', arch = 'amd64'] = platform.split('_')
      return {
        os,
        arch,
        filename: `terraform-provider-inventory_${version}_${platform}.zip`,
        size: 9_600 * KiB,
        sha256: fakeSha256(`inventory/${version}/${platform}`),
        h1: `h1:${btoa(fakeSha256(`h1/inventory/${version}/${platform}`).slice(0, 32)).slice(0, 43)}=`,
        cached_at: new Date(NOW - (i + 1) * 9 * DAY).toISOString(),
        last_downloaded_at: new Date(NOW - (i + 1) * DAY).toISOString(),
        downloads: 40 - i * 25,
      }
    })
    return {
      version,
      prerelease: false,
      protocols: ['6.0'],
      platforms: ['darwin_arm64', 'linux_amd64'],
      listed: true,
      archives,
      size_bytes: archives.reduce((sum, a) => sum + a.size, 0),
      key_id: KEY_ID,
      uploaded_by: 'admin',
    }
  })
  return { provider: hostedProviderList()[0]!, versions }
}
