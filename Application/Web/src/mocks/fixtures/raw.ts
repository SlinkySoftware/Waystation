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

/** A folder of 100,000 files in `raw-releases` (T-U05 performance check: the tree browses it smoothly). Built once,
 * on first use; deterministic names and sizes. */
export const BIG_FOLDER = 'mirror/nightly'
export const BIG_FOLDER_SIZE = 100_000
let bigFolder: FolderEntry[] | null = null

function bigFolderEntries(): FolderEntry[] {
  if (bigFolder) return bigFolder
  const modified = new Date(NOW - 2 * DAY).toISOString()
  bigFolder = Array.from({ length: BIG_FOLDER_SIZE }, (_, i) => {
    const name = `build-${String(i + 1).padStart(6, '0')}.tar.gz`
    return {
      name,
      path: `${BIG_FOLDER}/${name}`,
      kind: 'file' as const,
      size: 1_048_576 + ((i * 7919) % 4_194_304),
      content_type: 'application/gzip',
      sha256: null,
      last_modified: modified,
      last_downloaded_at: null,
      download_count: i % 5,
      child_count: null,
    }
  })
  return bigFolder
}

/** Folder listing of `files` at `path` (folders first, then files; each by name); null when `path` is not a folder. */
function listingOf(files: [string, number][], path: string, seed: string, extraFolders: Record<string, number> = {}): FolderEntry[] | null {
  const prefix = path ? `${path.replace(/\/+$/, '')}/` : ''
  const folders = new Map<string, number>(Object.entries(extraFolders))
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
    const next = rng(hash(`${seed}:${file}`))
    entries.push({
      name: rest,
      path: file,
      kind: 'file',
      size: Math.round(size),
      content_type: contentType(rest),
      sha256: fakeSha256(`${seed.startsWith('raw:') ? 'raw' : seed}:${file}`),
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

function contentType(name: string): string {
  if (name.endsWith('.gz') || name.endsWith('.tgz')) return 'application/gzip'
  if (name.endsWith('.iso')) return 'application/x-iso9660-image'
  if (name.endsWith('.txt') || name.endsWith('SUMS') || name === 'InRelease' || name === 'Release') return 'text/plain'
  if (name.endsWith('.json') || name.startsWith('sha256:')) return 'application/json'
  if (name.endsWith('.deb')) return 'application/vnd.debian.binary-package'
  if (name.endsWith('.rpm')) return 'application/x-rpm'
  if (name.endsWith('.xml') || name.endsWith('.xml.gz')) return 'application/xml'
  return 'application/octet-stream'
}

export function rawListing(repo: string, path: string): FolderEntry[] | null {
  const files = RAW_FILES[repo]
  if (!files) return null
  if (repo === 'raw-releases') {
    if (path === BIG_FOLDER) return bigFolderEntries()
    if (path === '') return listingOf(files, path, `raw:${repo}`, { mirror: BIG_FOLDER_SIZE })
    if (path === 'mirror') return listingOf(files, path, `raw:${repo}`, { nightly: BIG_FOLDER_SIZE })
  }
  return listingOf(files, path, `raw:${repo}`)
}

/** What the generic tree (Browse tab) shows for the other formats: the storage layout of a few cached artifacts. */
const D = 'sha256:'
const GENERIC_FILES: Record<string, [string, number][]> = {
  docker: [
    [`v2/library/nginx/manifests/${D}2f9c3a1e6b0d`, 10 * KiB],
    [`v2/library/nginx/manifests/${D}8d7e4b5a9c21`, 2 * KiB],
    [`v2/library/nginx/blobs/${D}4f4fb700ef54`, 32],
    [`v2/library/nginx/blobs/${D}a2318d6c47ec`, 28.2 * MiB],
    [`v2/library/nginx/blobs/${D}b9c1a5f0e7d3`, 41.6 * MiB],
    [`v2/library/postgres/manifests/${D}5d1e9c0b7a64`, 9 * KiB],
    [`v2/library/postgres/blobs/${D}c3e8a9d7f1b2`, 96.3 * MiB],
    [`v2/library/alpine/manifests/${D}1c9a7e3d5b80`, 9 * KiB],
    [`v2/library/alpine/blobs/${D}e7f3c2a1b9d4`, 3.6 * MiB],
  ],
  npm: [
    ['lodash/-/lodash-4.17.21.tgz', 311 * KiB],
    ['vue/-/vue-3.5.24.tgz', 2.3 * MiB],
    ['vue/-/vue-3.5.22.tgz', 2.3 * MiB],
    ['@vue/reactivity/-/reactivity-3.5.24.tgz', 1.1 * MiB],
    ['@types/node/-/node-24.10.1.tgz', 1.6 * MiB],
    ['typescript/-/typescript-5.9.3.tgz', 4.4 * MiB],
  ],
  apt: [
    ['dists/trixie/InRelease', 151 * KiB],
    ['dists/trixie/main/binary-amd64/Packages.xz', 9.6 * MiB],
    ['dists/trixie/main/binary-arm64/Packages.xz', 9.4 * MiB],
    ['pool/main/o/openssl/libssl3t64_3.5.4-1_amd64.deb', 2.3 * MiB],
    ['pool/main/o/openssl/openssl_3.5.4-1_amd64.deb', 1.4 * MiB],
    ['pool/main/c/curl/curl_8.14.1-2_amd64.deb', 269 * KiB],
    ['pool/main/c/curl/libcurl4t64_8.14.1-2_amd64.deb', 380 * KiB],
  ],
  yum: [
    ['BaseOS/x86_64/os/repodata/repomd.xml', 4 * KiB],
    ['BaseOS/x86_64/os/repodata/primary.xml.gz', 2.1 * MiB],
    ['BaseOS/x86_64/os/Packages/o/openssl-3.2.2-6.el10.x86_64.rpm', 1.5 * MiB],
    ['AppStream/x86_64/os/repodata/repomd.xml', 4 * KiB],
  ],
}

/** Generic tree for non-raw formats (Browse tab); null when `path` is not a folder. */
export function genericListing(format: string, path: string): FolderEntry[] | null {
  const files = GENERIC_FILES[format]
  if (!files) return path ? null : []
  return listingOf(files, path, format)
}
