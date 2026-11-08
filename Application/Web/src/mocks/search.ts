// SPDX-License-Identifier: GPL-3.0-or-later
// Mock global search (GET /search) over the fixture content, modelled on the server engine (SPEC §4.6):
// name prefix on the full name or its last "/" segment, optional version prefix, digest lookup, disjunctive facets.
import type { Repository, SearchFacets, SearchFacetValue, SearchHit, SearchKind, SearchPage } from '@/api/types'

import { aptPackagesFor, aptSuiteNames, aptSuitesFor } from './fixtures/apt'
import { dockerImagesFor, dockerTagsFor } from './fixtures/docker'
import { npmPackageList, tarballPath } from './fixtures/npm'
import { RAW_FILES } from './fixtures/raw'
import { YUM_DISTROS, yumPackages } from './fixtures/yum'
import { fakeSha256, hash, rng } from './rng'

export interface SearchParams {
  q: string
  formats: string[]
  repositories: string[]
  kinds: string[]
  types: string[]
  cached: boolean
}

interface Entry {
  hit: SearchHit
  keys: string[]
  rank: number
}

const DIGEST = /^(?:sha256:([0-9a-f]{8,64})|([0-9a-f]{64}))$/
const KIND_ORDER: SearchKind[] = ['image', 'package', 'file']
let cache: { repos: Repository[]; entries: Entry[] } | null = null

function parent(path: string): Record<string, string> {
  const i = path.lastIndexOf('/')
  return i > 0 ? { path: path.slice(0, i) } : {}
}

function keysOf(name: string): string[] {
  const lc = name.toLowerCase()
  const base = lc.slice(lc.lastIndexOf('/') + 1)
  return name.includes('/') && base && base !== lc ? [lc, base] : [lc]
}

function base(repo: Repository, kind: SearchKind): Pick<SearchHit, 'kind' | 'format' | 'repository' | 'repository_type'> {
  return { kind, format: repo.format, repository: repo.name, repository_type: repo.type }
}

function fileHit(repo: Repository, path: string, size: number, sha: string, updated: string, browse: Record<string, string>): Entry {
  const name = path.slice(path.lastIndexOf('/') + 1)
  return {
    hit: { ...base(repo, 'file'), name, version: null, qualifier: null, path, size, sha256: sha, updated_at: updated, cached: true, browse: { repository: repo.name, query: browse } },
    keys: [name.toLowerCase()],
    rank: 6,
  }
}

function entriesFor(repo: Repository): Entry[] {
  const out: Entry[] = []
  const next = rng(hash(`search:${repo.name}`))
  const when = () => new Date(Date.now() - next() * 20 * 86_400_000).toISOString()
  if (repo.format === 'docker') {
    for (const image of dockerImagesFor(repo.name)) {
      for (const tag of dockerTagsFor(repo.name, image.name) ?? []) {
        out.push({
          hit: { ...base(repo, 'image'), name: image.name, version: tag.tag, qualifier: null, path: null, size: tag.size_bytes, sha256: tag.digest, updated_at: tag.updated_at, cached: true, browse: { repository: repo.name, query: { image: image.name, ref: tag.tag } } },
          keys: keysOf(image.name),
          rank: 0,
        })
      }
    }
  } else if (repo.format === 'npm') {
    for (const pkg of npmPackageList()) {
      out.push({
        hit: { ...base(repo, 'package'), name: pkg.name, version: pkg.latest, qualifier: null, path: null, size: pkg.size_bytes, sha256: null, updated_at: pkg.last_downloaded_at ?? null, cached: true, browse: { repository: repo.name, query: { package: pkg.name } } },
        keys: keysOf(pkg.name),
        rank: 2,
      })
      const path = tarballPath(pkg.name, pkg.latest)
      out.push(fileHit(repo, path, Math.round(pkg.size_bytes / Math.max(1, pkg.cached_versions)), fakeSha256(`tgz:${path}`), when(), { package: pkg.name }))
    }
  } else if (repo.format === 'apt') {
    for (const suite of aptSuiteNames(repo.name)) {
      const archs = (aptSuitesFor(repo.name).find((s) => s.suite === suite)?.architectures ?? []).filter((a) => a !== 'all')
      for (const arch of archs) {
        for (const p of aptPackagesFor(repo.name, suite, arch)) {
          out.push({
            hit: { ...base(repo, 'package'), name: p.name, version: p.version, qualifier: `${suite}/${p.component} ${arch}`, path: p.filename, size: p.size ?? null, sha256: p.sha256 ?? null, updated_at: p.last_downloaded_at ?? null, cached: p.cached, browse: { repository: repo.name, query: { suite, arch, q: p.name } } },
            keys: [p.name.toLowerCase()],
            rank: 4,
          })
          if (p.cached) out.push(fileHit(repo, p.filename, p.size ?? 0, p.sha256 ?? '', p.last_downloaded_at ?? when(), {}))
        }
      }
    }
  } else if (repo.format === 'yum' && YUM_DISTROS[repo.name]) {
    for (const arch of YUM_DISTROS[repo.name]!.archs) {
      for (const file of yumPackages(repo.name, arch)) {
        const [, name = file.name, version = ''] = /^(.+?)-(\d.*)$/.exec(file.name) ?? []
        const rpmArch = file.path.slice(file.path.lastIndexOf('.', file.path.length - 5) + 1, -4)
        const cached = next() < 0.4
        out.push({
          hit: { ...base(repo, 'package'), name, version, qualifier: rpmArch, path: file.path, size: file.size, sha256: null, updated_at: when(), cached, browse: { repository: repo.name, query: parent(file.path) } },
          keys: [name.toLowerCase()],
          rank: 5,
        })
        if (cached) out.push(fileHit(repo, file.path, file.size, fakeSha256(`rpm:${file.path}`), when(), parent(file.path)))
      }
    }
  } else if (repo.format === 'raw') {
    for (const [path, size] of RAW_FILES[repo.name] ?? []) out.push(fileHit(repo, path, Math.round(size), fakeSha256(`raw:${path}`), when(), parent(path)))
  }
  return out
}

function index(repos: Repository[]): Entry[] {
  if (!cache || cache.repos !== repos) cache = { repos, entries: repos.flatMap(entriesFor) }
  return cache.entries
}

function passes(p: SearchParams, hit: SearchHit, skip = ''): boolean {
  return (
    (skip === 'format' || !p.formats.length || p.formats.includes(hit.format)) &&
    (skip === 'repository' || !p.repositories.length || p.repositories.includes(hit.repository)) &&
    (skip === 'type' || !p.types.length || p.types.includes(hit.repository_type)) &&
    (skip === 'kind' || !p.kinds.length || p.kinds.includes(hit.kind))
  )
}

function facets(p: SearchParams, matches: SearchHit[]): SearchFacets {
  const tally = (skip: string, value: (h: SearchHit) => string, selected: string[]): SearchFacetValue[] => {
    const counts = new Map<string, number>(selected.map((s) => [s, 0]))
    for (const h of matches) if (passes(p, h, skip)) counts.set(value(h), (counts.get(value(h)) ?? 0) + 1)
    return [...counts].map(([v, count]) => ({ value: v, count })).sort((a, b) => b.count - a.count || a.value.localeCompare(b.value))
  }
  return {
    format: tally('format', (h) => h.format, p.formats),
    repository: tally('repository', (h) => h.repository, p.repositories),
    kind: tally('kind', (h) => h.kind, p.kinds),
    type: tally('type', (h) => h.repository_type, p.types),
    total: matches.filter((h) => passes(p, h)).length,
    capped: false,
  }
}

/** Every match of `p` in result order (before filters), and the search mode. */
export function searchAll(repos: Repository[], p: SearchParams): { mode: SearchPage['mode']; matches: SearchHit[] } {
  const text = p.q.trim()
  const digest = DIGEST.exec(text.toLowerCase())
  const entries = index(repos)
  if (digest) {
    const hex = digest[1] ?? digest[2]!
    const matches = entries
      .filter((e) => e.hit.kind !== 'package' && (e.hit.sha256 ?? '').replace(/^sha256:/, '').startsWith(hex))
      .map((e) => e.hit)
      .sort((a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) || a.repository.localeCompare(b.repository) || a.name.localeCompare(b.name))
    return { mode: 'digest', matches }
  }
  const [rawName = '', ...rest] = text.split(/\s+/)
  const name = rawName.toLowerCase()
  const version = rest.join(' ') || null
  const found: { key: string; rank: number; hit: SearchHit }[] = []
  for (const e of entries) {
    if (p.cached && e.hit.kind === 'package' && (e.hit.format === 'apt' || e.hit.format === 'yum')) continue
    if (version !== null && !(e.hit.version ?? '').startsWith(version)) continue
    if (version !== null && e.hit.kind === 'file') continue
    const key = e.keys.find((k) => k.startsWith(name))
    if (key !== undefined) found.push({ key, rank: e.rank + (key === e.keys[0] ? 0 : 1), hit: e.hit })
  }
  found.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : a.rank - b.rank || a.hit.repository.localeCompare(b.hit.repository)))
  return { mode: 'name', matches: found.map((f) => f.hit) }
}

export function mockSearch(repos: Repository[], p: SearchParams, offset: number, limit: number, withFacets: boolean): SearchPage {
  const { mode, matches } = searchAll(repos, p)
  const filtered = matches.filter((h) => passes(p, h))
  const items = filtered.slice(offset, offset + limit)
  return {
    mode,
    items,
    next_cursor: offset + limit < filtered.length ? btoa(String(offset + limit)) : null,
    facets: withFacets && offset === 0 ? facets(p, matches) : null,
  }
}

