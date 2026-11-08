// SPDX-License-Identifier: GPL-3.0-or-later
// Files cached by the raw proxy (the example GitHub releases repo), as a folder tree (paths mirror the upstream layout).
import type { FolderEntry } from '@/api/types'

import { fakeSha256, hash, KiB, MiB, rng } from '../rng'

const NOW = Date.now()
const DAY = 86_400_000

export const RAW_FILES: Record<string, [string, number][]> = {
  'raw-releases': [
    ['cli/cli/releases/download/v2.83.0/gh_2.83.0_linux_amd64.tar.gz', 13.4 * MiB],
    ['cli/cli/releases/download/v2.83.0/gh_2.83.0_linux_arm64.tar.gz', 12.2 * MiB],
    ['cli/cli/releases/download/v2.82.1/gh_2.82.1_linux_amd64.tar.gz', 13.3 * MiB],
    ['docker/compose/releases/download/v2.40.3/docker-compose-linux-x86_64', 72.8 * MiB],
    ['docker/compose/releases/download/v2.40.3/docker-compose-linux-aarch64', 69.1 * MiB],
    ['kubernetes-sigs/kind/releases/download/v0.30.0/kind-linux-amd64', 10.6 * MiB],
    ['derailed/k9s/releases/download/v0.50.16/k9s_Linux_amd64.tar.gz', 37.9 * MiB],
    ['prometheus/node_exporter/releases/download/v1.10.2/node_exporter-1.10.2.linux-amd64.tar.gz', 11.5 * MiB],
    ['prometheus/node_exporter/releases/download/v1.10.2/node_exporter-1.10.2.linux-arm64.tar.gz', 10.6 * MiB],
    ['jqlang/jq/releases/download/jq-1.8.1/jq-linux-amd64', 2.2 * MiB],
    ['astral-sh/uv/releases/download/0.9.8/uv-x86_64-unknown-linux-gnu.tar.gz', 19.6 * MiB],
    ['astral-sh/uv/releases/download/0.9.8/uv-aarch64-unknown-linux-gnu.tar.gz', 18.4 * MiB],
    ['mikefarah/yq/releases/download/v4.48.1/yq_linux_amd64', 11.2 * MiB],
    ['helmfile/helmfile/releases/download/v1.1.9/helmfile_1.1.9_linux_amd64.tar.gz', 28.7 * MiB],
    ['restic/restic/releases/download/v0.18.1/restic_0.18.1_linux_amd64.bz2', 9.4 * MiB],
    ['gohugoio/hugo/releases/download/v0.152.2/hugo_extended_0.152.2_linux-amd64.tar.gz', 24.8 * MiB],
    ['cli/cli/releases/download/v2.83.0/gh_2.83.0_checksums.txt', 2.1 * KiB],
  ],
}

export function rawListing(repo: string, path: string): FolderEntry[] | null {
  const files = RAW_FILES[repo]
  if (!files) return null
  const prefix = path ? `${path.replace(/\/+$/, '')}/` : ''
  const folders = new Map<string, number>()
  const entries: FolderEntry[] = []
  for (const [file, size] of files) {
    if (!file.startsWith(prefix)) continue
    const rest = file.slice(prefix.length)
    const slash = rest.indexOf('/')
    if (slash >= 0) {
      const name = rest.slice(0, slash)
      folders.set(name, (folders.get(name) ?? 0) + 1)
      continue
    }
    const next = rng(hash(`raw:${repo}:${file}`))
    entries.push({
      name: rest,
      path: file,
      kind: 'file',
      size: Math.round(size),
      content_type: rest.endsWith('.gz') ? 'application/gzip' : rest.endsWith('.iso') ? 'application/x-iso9660-image' : rest.endsWith('.txt') || rest.endsWith('SUMS') ? 'text/plain' : 'application/octet-stream',
      sha256: fakeSha256(`raw:${file}`),
      last_modified: new Date(NOW - (3 + next() * 60) * DAY).toISOString(),
      last_downloaded_at: new Date(NOW - next() * 6 * DAY).toISOString(),
      download_count: Math.round(1 + next() * 120),
      child_count: null,
    })
  }
  if (!entries.length && !folders.size && path) return null
  const folderEntries: FolderEntry[] = [...folders].map(([name, count]) => ({
    name,
    path: `${prefix}${name}`,
    kind: 'folder',
    size: null,
    child_count: count,
  }))
  folderEntries.sort((a, b) => a.name.localeCompare(b.name))
  entries.sort((a, b) => a.name.localeCompare(b.name))
  return [...folderEntries, ...entries]
}

/** Generic tree for non-raw formats (Browse > Tree): a few top-level folders. */
export function genericTree(format: string): FolderEntry[] {
  const names: Record<string, string[]> = {
    docker: ['v2'],
    npm: ['@babel', '@types', '@vue', 'lodash', 'react', 'typescript', 'vue'],
    apt: ['dists', 'pool'],
  }
  return (names[format] ?? []).map((name) => ({ name, path: name, kind: 'folder', size: null, child_count: null }))
}
