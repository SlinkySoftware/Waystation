// SPDX-License-Identifier: GPL-3.0-or-later
// PyPI projects for any PyPI repository created in mock mode: real project names, plausible versions, wheels and
// sdists. Hosted repositories show an uploader; yanks are remembered per repository for the session.
import type { PypiFile, PypiProject, PypiVersion } from '@/api/types'

import { fakeSha256, hash, KiB, rng } from '../rng'

const NOW = Date.now()
const DAY = 86_400_000

interface ProjectSeed {
  name: string
  summary: string
  latest: string
  kib: number
  python: string
  versions: number
}

const PROJECTS: ProjectSeed[] = [
  { name: 'requests', summary: 'Python HTTP for Humans.', latest: '2.32.5', kib: 63, python: '>=3.9', versions: 157 },
  { name: 'urllib3', summary: 'HTTP library with thread-safe connection pooling, file post, and more.', latest: '2.5.0', kib: 126, python: '>=3.9', versions: 112 },
  { name: 'certifi', summary: 'Python package for providing Mozilla\'s CA Bundle.', latest: '2025.10.5', kib: 160, python: '>=3.7', versions: 80 },
  { name: 'idna', summary: 'Internationalized Domain Names in Applications (IDNA)', latest: '3.11', kib: 70, python: '>=3.8', versions: 40 },
  { name: 'charset-normalizer', summary: 'The Real First Universal Charset Detector.', latest: '3.4.4', kib: 140, python: '>=3.7', versions: 75 },
  { name: 'packaging', summary: 'Core utilities for Python packages', latest: '25.0', kib: 66, python: '>=3.8', versions: 60 },
  { name: 'six', summary: 'Python 2 and 3 compatibility utilities', latest: '1.17.0', kib: 11, python: '>=2.7', versions: 30 },
  { name: 'pydantic', summary: 'Data validation using Python type hints', latest: '2.12.4', kib: 460, python: '>=3.9', versions: 170 },
  { name: 'fastapi', summary: 'FastAPI framework, high performance, easy to learn, fast to code, ready for production', latest: '0.121.1', kib: 108, python: '>=3.8', versions: 290 },
  { name: 'numpy', summary: 'Fundamental package for array computing in Python', latest: '2.3.4', kib: 16_400, python: '>=3.11', versions: 120 },
  { name: 'ansible-core', summary: 'Radically simple IT automation', latest: '2.19.4', kib: 2300, python: '>=3.11', versions: 140 },
  { name: 'pyyaml', summary: 'YAML parser and emitter for Python', latest: '6.0.3', kib: 760, python: '>=3.8', versions: 45 },
]

const yanked = new Map<string, string>() // `${repo}/${project}/${version}` -> reason

function versionList(seed: ProjectSeed, count: number): string[] {
  const parts = seed.latest.split('.').map((p) => Number.parseInt(p, 10))
  const out: string[] = []
  const next = rng(hash(`pyver:${seed.name}`))
  let [a = 1, b = 0, c = 0] = parts
  while (out.length < count && a >= 0) {
    out.push(parts.length === 2 ? `${a}.${b}` : `${a}.${b}.${c}`)
    if (parts.length === 3 && c > 0) c--
    else if (b > 0) {
      b--
      c = Math.floor(next() * 5)
    } else {
      a--
      b = Math.floor(next() * 9)
    }
  }
  return out
}

function stats(seed: ProjectSeed): { files: number; size: number } {
  const next = rng(hash(`pyproj:${seed.name}`))
  const files = 1 + Math.floor(next() * 6)
  return { files, size: Math.round(seed.kib * KiB * files * (0.8 + next() * 0.3)) }
}

export function pypiProjectList(): PypiProject[] {
  return PROJECTS.map((seed) => {
    const next = rng(hash(`pylist:${seed.name}`))
    const held = stats(seed)
    return {
      name: seed.name,
      display_name: seed.name,
      summary: seed.summary,
      latest: seed.latest,
      version_count: seed.versions,
      files: held.files,
      size_bytes: held.size,
      last_downloaded_at: new Date(NOW - next() * 3 * DAY).toISOString(),
      downloads: Math.round(20 + next() * 6000),
    }
  })
}

export function pypiVersionsFor(repo: string, hosted: boolean, project: string): { project: PypiProject; versions: PypiVersion[] } | null {
  const name = project.toLowerCase().replace(/[-_.]+/g, '-')
  const seed = PROJECTS.find((p) => p.name === name)
  const summary = pypiProjectList().find((p) => p.name === name)
  if (!seed || !summary) return null
  const next = rng(hash(`pyvers:${name}`))
  let when = NOW - next() * 10 * DAY
  const versions = versionList(seed, Math.min(30, seed.versions)).map((version, i): PypiVersion => {
    when -= (2 + next() * 30) * DAY
    const dist = name.replace(/-/g, '_')
    const reason = yanked.get(`${repo}/${name}/${version}`)
    const file = (filename: string, kind: PypiFile['kind'], size: number, cached: boolean): PypiFile => ({
      filename,
      kind,
      size,
      sha256: fakeSha256(`${repo}/${filename}`),
      requires_python: seed.python,
      yanked: reason !== undefined,
      yanked_reason: reason || null,
      core_metadata: kind === 'wheel',
      cached,
      uploaded_at: hosted ? new Date(when).toISOString() : null,
      uploaded_by: hosted ? 'ci-builder' : null,
      last_downloaded_at: cached ? new Date(NOW - next() * 4 * DAY).toISOString() : null,
      downloads: cached ? Math.round(next() * 900 / (i + 1)) : 0,
    })
    const size = Math.round(seed.kib * KiB * (0.95 + next() * 0.1))
    const files = [
      file(`${dist}-${version}.tar.gz`, 'sdist', Math.round(size * 1.6), hosted || i < 2),
      file(`${dist}-${version}-py3-none-any.whl`, 'wheel', size, hosted || i < 3),
    ]
    const held = files.filter((f) => f.cached)
    return {
      version,
      prerelease: false,
      yanked: reason !== undefined,
      files,
      cached_files: held.length,
      size_bytes: held.reduce((sum, f) => sum + (f.size ?? 0), 0),
    }
  })
  return { project: summary, versions }
}

/** Yank or un-yank a version (hosted repositories); false when the project or version is unknown. */
export function pypiYank(repo: string, project: string, version: string, reason: string | null, yank: boolean): boolean {
  const name = project.toLowerCase().replace(/[-_.]+/g, '-')
  const seed = PROJECTS.find((p) => p.name === name)
  if (!seed || !versionList(seed, Math.min(30, seed.versions)).includes(version)) return false
  const key = `${repo}/${name}/${version}`
  if (yank) yanked.set(key, reason ?? '')
  else yanked.delete(key)
  return true
}
