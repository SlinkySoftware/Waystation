// SPDX-License-Identifier: GPL-3.0-or-later
// Ansible collections for any Galaxy repository created in the mock: real public collection names, plausible
// versions, sizes and dependencies.
import type { GalaxyCollection, GalaxyNamespace, GalaxyVersion } from '@/api/types'

import { hash, KiB } from '../rng'
import { BASE_URL } from './repositories'

const NOW = Date.now()
const DAY = 86_400_000

interface CollectionSeed {
  namespace: string
  name: string
  description: string
  requires: string
  /** Newest first (SemVer precedence). */
  versions: string[]
  kib: number
  dependencies?: Record<string, string>
}

const COLLECTIONS: CollectionSeed[] = [
  { namespace: 'ansible', name: 'posix', description: 'Ansible Collection targeting POSIX and POSIX-ish platforms.', requires: '>=2.16.0', versions: ['2.1.0', '2.0.0', '1.6.2', '1.6.1'], kib: 132 },
  { namespace: 'ansible', name: 'utils', description: 'Ansible Collection with utilities to ease the management, manipulation, and validation of data within a playbook', requires: '>=2.16.0', versions: ['6.0.0', '5.1.2', '5.1.1'], kib: 212 },
  { namespace: 'ansible', name: 'netcommon', description: 'Ansible Collection with common content to help automate the management of network, security, and cloud devices.', requires: '>=2.16.0', versions: ['8.1.0', '8.0.1', '7.2.0'], kib: 178, dependencies: { 'ansible.utils': '>=6.0.0' } },
  { namespace: 'community', name: 'general', description: 'The community.general collection is a part of the Ansible package and includes many modules and plugins supported by Ansible community.', requires: '>=2.17.0', versions: ['12.0.1', '12.0.0', '11.4.1', '11.4.0', '10.7.5'], kib: 2860 },
  { namespace: 'community', name: 'docker', description: 'Modules and plugins for working with Docker', requires: '>=2.17.0', versions: ['5.0.0', '4.8.2', '4.8.1'], kib: 396, dependencies: { 'community.library_inventory_filtering_v1': '>=1.0.0' } },
  { namespace: 'community', name: 'library_inventory_filtering_v1', description: 'Library for inventory plugins in other collections that allows to filter hosts in a generic way.', requires: '>=2.9.10', versions: ['1.1.5', '1.1.4', '1.1.3'], kib: 36 },
  { namespace: 'community', name: 'crypto', description: 'The community.crypto collection for Ansible.', requires: '>=2.17.0', versions: ['3.0.4', '3.0.3', '2.26.5'], kib: 520, dependencies: {} },
  { namespace: 'kubernetes', name: 'core', description: 'Kubernetes Collection for Ansible.', requires: '>=2.16.0', versions: ['6.2.0', '6.1.0', '6.0.0-rc.1', '5.4.0'], kib: 254 },
]

function collection(seed: CollectionSeed): GalaxyCollection {
  return { namespace: seed.namespace, name: seed.name, latest_version: seed.versions[0] ?? '', description: seed.description, requires_ansible: seed.requires, version_count: seed.versions.length }
}

export function galaxyNamespaceList(): GalaxyNamespace[] {
  const counts = new Map<string, number>()
  for (const c of COLLECTIONS) counts.set(c.namespace, (counts.get(c.namespace) ?? 0) + 1)
  return [...counts].map(([namespace, count]) => ({ namespace, collection_count: count })).sort((a, b) => a.namespace.localeCompare(b.namespace))
}

export function galaxyCollectionList(namespace: string | null): GalaxyCollection[] {
  return COLLECTIONS.filter((c) => !namespace || c.namespace === namespace)
    .map(collection)
    .sort((a, b) => `${a.namespace}.${a.name}`.localeCompare(`${b.namespace}.${b.name}`))
}

export function galaxyVersionsFor(repo: string, hosted: boolean, namespace: string, name: string): { collection: GalaxyCollection; versions: GalaxyVersion[] } | null {
  const seed = COLLECTIONS.find((c) => c.namespace === namespace && c.name === name)
  if (!seed) return null
  const versions = seed.versions.map((version, i): GalaxyVersion => {
    const h = hash(`${repo}/${namespace}.${name}/${version}`)
    const path = `collections/${namespace}/${name}/${namespace}-${name}-${version}.tar.gz`
    const cached = hosted || i < 2
    return {
      version,
      prerelease: version.includes('-'),
      created_at: new Date(NOW - (i * 31 + (h % 11)) * DAY).toISOString(),
      requires_ansible: seed.requires,
      sha256: cached || i < 3 ? h.toString(16).padStart(8, '0').repeat(8) : null,
      size_bytes: Math.round(seed.kib * KiB * (0.9 + (h % 20) / 100)),
      cached,
      dependencies: seed.dependencies ?? {},
      uploaded_by: hosted ? 'ci-bot' : null,
      path,
      download_url: `${BASE_URL}/repository/${repo}/${path}`,
    }
  })
  return { collection: collection(seed), versions }
}
