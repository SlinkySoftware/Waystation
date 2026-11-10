// SPDX-License-Identifier: GPL-3.0-or-later
// Terraform providers for any provider mirror created in mock mode: real public provider addresses from both origin
// registries, their listed versions and the linux_amd64 / darwin_arm64 archives a team's runs cached.
import type { TerraformArchive, TerraformProvider, TerraformVersion } from '@/api/types'

import { fakeSha256, hash, KiB, rng } from '../rng'

const NOW = Date.now()
const DAY = 86_400_000
const PLATFORMS = ['darwin_amd64', 'darwin_arm64', 'freebsd_amd64', 'linux_386', 'linux_amd64', 'linux_arm', 'linux_arm64', 'windows_386', 'windows_amd64']

interface ProviderSeed {
  provider: string
  versions: string[] // newest first
  kib: number // archive size
  cached: number // newest versions with cached archives
  platforms: string[] // cached platforms
}

const PROVIDERS: ProviderSeed[] = [
  { provider: 'registry.terraform.io/hashicorp/aws', versions: ['6.18.0', '6.17.0', '6.16.0', '6.15.0', '5.100.0'], kib: 160_000, cached: 2, platforms: ['linux_amd64'] },
  { provider: 'registry.terraform.io/hashicorp/random', versions: ['3.7.2', '3.7.1', '3.7.0', '3.6.3'], kib: 6_400, cached: 1, platforms: ['linux_amd64', 'darwin_arm64'] },
  { provider: 'registry.terraform.io/hashicorp/null', versions: ['3.2.4', '3.2.3', '3.2.2', '3.1.0'], kib: 5_600, cached: 2, platforms: ['linux_amd64', 'darwin_arm64'] },
  { provider: 'registry.terraform.io/hashicorp/kubernetes', versions: ['2.38.0', '2.37.1', '2.36.0'], kib: 32_000, cached: 1, platforms: ['linux_amd64'] },
  { provider: 'registry.terraform.io/bpg/proxmox', versions: ['0.86.0', '0.85.1', '0.85.0', '0.84.1-rc1'], kib: 18_000, cached: 1, platforms: ['linux_amd64'] },
  { provider: 'registry.opentofu.org/hashicorp/null', versions: ['3.2.4', '3.2.3', '3.1.0'], kib: 5_500, cached: 1, platforms: ['linux_amd64'] },
  { provider: 'registry.opentofu.org/hashicorp/local', versions: ['2.5.3', '2.5.2', '2.5.1'], kib: 5_700, cached: 1, platforms: ['linux_amd64'] },
]

const PRERELEASE = /-/

function archiveSize(seed: ProviderSeed, version: string, platform: string): number {
  const next = rng(hash(`tfzip:${seed.provider}@${version}:${platform}`))
  return Math.round(seed.kib * KiB * (0.9 + next() * 0.15))
}

function cachedPlatforms(seed: ProviderSeed, index: number): string[] {
  return index < seed.cached ? seed.platforms : []
}

function summary(seed: ProviderSeed): TerraformProvider {
  const held = seed.versions.flatMap((v, i) => cachedPlatforms(seed, i).map((p) => archiveSize(seed, v, p)))
  return {
    provider: seed.provider,
    latest: seed.versions.find((v) => !PRERELEASE.test(v)) ?? seed.versions[0] ?? null,
    version_count: seed.versions.length,
    cached_archives: held.length,
    size_bytes: held.reduce((sum, size) => sum + size, 0),
  }
}

export function terraformProviderList(): TerraformProvider[] {
  return [...PROVIDERS].sort((a, b) => a.provider.localeCompare(b.provider)).map(summary)
}

function h1(seed: string): string {
  return `h1:${btoa(fakeSha256(seed).slice(0, 32)).slice(0, 43)}=`
}

export function terraformVersionsFor(repo: string, provider: string): { provider: TerraformProvider; versions: TerraformVersion[] } | null {
  const seed = PROVIDERS.find((p) => p.provider === provider.trim())
  if (!seed) return null
  const next = rng(hash(`tfver:${repo}:${seed.provider}`))
  const type = seed.provider.split('/')[2]
  const versions = seed.versions.map((version, i): TerraformVersion => {
    const archives = cachedPlatforms(seed, i).map((platform): TerraformArchive => {
      const [os = 'linux', arch = 'amd64'] = platform.split('_')
      return {
        os,
        arch,
        filename: `terraform-provider-${type}_${version}_${platform}.zip`,
        size: archiveSize(seed, version, platform),
        sha256: fakeSha256(`${repo}/${seed.provider}/${version}/${platform}`),
        h1: h1(`${seed.provider}/${version}/${platform}`),
        cached_at: new Date(NOW - next() * 30 * DAY).toISOString(),
        last_downloaded_at: new Date(NOW - next() * 4 * DAY).toISOString(),
        downloads: Math.round(next() * 300),
      }
    })
    return {
      version,
      prerelease: PRERELEASE.test(version),
      protocols: ['5.0'],
      platforms: PLATFORMS,
      listed: true,
      archives,
      size_bytes: archives.reduce((sum, a) => sum + a.size, 0),
    }
  })
  return { provider: summary(seed), versions }
}
