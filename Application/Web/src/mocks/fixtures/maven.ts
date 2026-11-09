// SPDX-License-Identifier: GPL-3.0-or-later
// What a Maven repository holds in mock mode (any maven repository): well-known public artifacts from Maven Central,
// a few versions each (hosted repositories: one example SNAPSHOT), and the files of a version folder.
import type { FolderEntry, MavenArtifact, MavenVersion, Repository } from '@/api/types'

import { fakeSha256, hash, KiB, rng } from '../rng'

const NOW = Date.now()
const DAY = 86_400_000

interface Coordinates {
  group: string
  artifact: string
  versions: string[]
  jar: number
}

const CENTRAL: Coordinates[] = [
  { group: 'com.fasterxml.jackson.core', artifact: 'jackson-databind', versions: ['2.19.2', '2.20.0', '2.20.1'], jar: 1650 * KiB },
  { group: 'com.google.guava', artifact: 'guava', versions: ['33.4.8-jre', '33.5.0-jre'], jar: 3050 * KiB },
  { group: 'junit', artifact: 'junit', versions: ['4.13.2'], jar: 376 * KiB },
  { group: 'org.apache.commons', artifact: 'commons-lang3', versions: ['3.18.0', '3.19.0'], jar: 690 * KiB },
  { group: 'org.junit.jupiter', artifact: 'junit-jupiter-api', versions: ['5.13.4', '6.0.1'], jar: 220 * KiB },
  { group: 'org.slf4j', artifact: 'slf4j-api', versions: ['2.0.17'], jar: 69 * KiB },
  { group: 'org.springframework', artifact: 'spring-core', versions: ['6.2.12', '7.0.0'], jar: 2010 * KiB },
]

const HOSTED: Coordinates[] = [{ group: 'com.example.platform', artifact: 'billing-service', versions: ['1.4.0', '1.4.1', '1.5.0-SNAPSHOT'], jar: 84 * KiB }]

function catalogue(repo: Repository): Coordinates[] {
  return repo.type === 'hosted' ? HOSTED : repo.type === 'proxy' ? CENTRAL : []
}

const folderOf = (c: Coordinates, v: string) => `${c.group.replaceAll('.', '/')}/${c.artifact}/${v}`

function fileNames(c: Coordinates, v: string): [string, number][] {
  const base = v.endsWith('-SNAPSHOT') ? `${c.artifact}-${v.replace('-SNAPSHOT', '')}-20261108.093015-4` : `${c.artifact}-${v}`
  const files: [string, number][] = [
    [`${base}.pom`, 6 * KiB],
    [`${base}.jar`, c.jar],
    [`${base}-sources.jar`, Math.round(c.jar * 0.6)],
  ]
  return [...files, ...files.map(([name]) => [`${name}.sha1`, 40] as [string, number])]
}

export function mavenArtifactsFor(repo: Repository): MavenArtifact[] {
  return catalogue(repo).map((c) => ({
    group_id: c.group,
    artifact_id: c.artifact,
    latest: c.versions[c.versions.length - 1] ?? null,
    version_count: c.versions.length,
    updated_at: new Date(NOW - rng(hash(`maven:${c.artifact}`))() * 20 * DAY).toISOString(),
  }))
}

export function mavenVersionsFor(repo: Repository, group: string, artifact: string): MavenVersion[] | null {
  const c = catalogue(repo).find((x) => x.group === group && x.artifact === artifact)
  if (!c) return null
  return [...c.versions].reverse().map((v, i) => {
    const files = fileNames(c, v)
    return {
      version: v,
      snapshot: v.endsWith('-SNAPSHOT'),
      path: folderOf(c, v),
      file_count: files.length,
      size_bytes: files.reduce((sum, [, size]) => sum + size, 0),
      created_at: new Date(NOW - (i * 30 + 5) * DAY).toISOString(),
      updated_at: new Date(NOW - (i * 30 + 1) * DAY).toISOString(),
      uploaded_by: repo.type === 'hosted' ? 'ci-builds' : null,
    }
  })
}

/** The files of a version folder, for the generic folder listing; null when the path is not one. */
export function mavenListing(repo: Repository, path: string): FolderEntry[] | null {
  for (const c of catalogue(repo)) {
    for (const v of c.versions) {
      if (folderOf(c, v) !== path.replace(/\/+$/, '')) continue
      return fileNames(c, v).map(([name, size]) => ({
        name,
        path: `${folderOf(c, v)}/${name}`,
        kind: 'file',
        size,
        content_type: name.endsWith('.jar') ? 'application/java-archive' : name.endsWith('.pom') ? 'application/xml' : 'text/plain',
        sha256: fakeSha256(`maven:${name}`),
        last_modified: new Date(NOW - 3 * DAY).toISOString(),
        last_downloaded_at: null,
        download_count: null,
        child_count: null,
      }))
    }
  }
  return null
}
