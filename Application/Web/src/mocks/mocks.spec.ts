// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { mavenArtifactsFor, mavenListing, mavenVersionsFor } from './fixtures/maven'
import { quotaView, seedRepositories } from './fixtures/repositories'
import { dnfRun } from './scenarios'
import { setupGuide } from './setup'

const repos = seedRepositories()
const byName = (name: string) => repos.find((r) => r.name === name)!

describe('mock repository set', () => {
  it('seeds the home-lab proxies with public upstreams only', () => {
    expect(repos.map((r) => r.name).sort()).toEqual(
      ['almalinux', 'debian', 'debian-security', 'dockerhub-proxy', 'epel', 'ghcr-proxy', 'npm', 'oracle', 'raspberrypi', 'raw-releases', 'rocky', 'ubuntu', 'ubuntu-security'].sort(),
    )
    for (const r of repos) expect(r.type).toBe('proxy')
    expect(repos.filter((r) => r.format === 'raw')).toHaveLength(1)
  })

  it('shows every health state that matters: epel blocked, oracle degraded, the rest healthy', () => {
    expect(byName('epel').status).toBe('blocked')
    expect(byName('oracle').status).toBe('degraded')
    expect(repos.filter((r) => r.status !== 'ok').map((r) => r.name).sort()).toEqual(['epel', 'oracle'])
  })

  it('uses the configured freshness', () => {
    expect(byName('dockerhub-proxy').proxy?.metadata_max_age_s).toBe(360 * 60)
    expect(byName('dockerhub-proxy').proxy?.negative_ttl_s).toBe(3600)
    expect(byName('debian').proxy?.metadata_max_age_s).toBe(3600)
    expect(byName('debian-security').proxy?.metadata_max_age_s).toBe(1800)
    expect(byName('ubuntu-security').proxy?.metadata_max_age_s).toBe(1800)
    expect(byName('npm').proxy?.metadata_max_age_s).toBe(1800)
    expect(byName('rocky').proxy?.negative_ttl_s).toBe(600)
  })
})

describe('mock quotas', () => {
  it('gives npm a soft quota past the warning level and raw-releases a hard one, the rest none', () => {
    expect(byName('npm').quota).toMatchObject({ mode: 'soft', state: 'warning' })
    expect(byName('npm').quota!.used_ratio).toBeGreaterThanOrEqual(0.8)
    expect(byName('raw-releases').quota).toMatchObject({ mode: 'hard', state: 'ok' })
    expect(repos.filter((r) => r.quota).map((r) => r.name).sort()).toEqual(['npm', 'raw-releases'])
    expect(byName('debian').storage).toEqual({ quota_bytes: null, quota_mode: 'soft' })
  })

  it('computes the quota state like the server', () => {
    expect(quotaView(undefined, 10)).toBeNull()
    expect(quotaView({ quota_bytes: 100 }, 79)?.state).toBe('ok')
    expect(quotaView({ quota_bytes: 100 }, 80)?.state).toBe('warning')
    expect(quotaView({ quota_bytes: 100, quota_mode: 'hard' }, 120)).toMatchObject({ state: 'exceeded', mode: 'hard', used_ratio: 1.2 })
  })
})

describe('mock Set me up', () => {
  const snippet = (repo: string, id: string) => setupGuide(byName(repo)).clients.flatMap((c) => c.snippets).find((s) => s.id === id)?.content ?? ''

  it('mirrors Docker Hub through the default repo and addresses ghcr by its upstream name', () => {
    expect(snippet('dockerhub-proxy', 'mirror')).toContain('registry-mirrors')
    expect(snippet('dockerhub-proxy', 'pull-default')).toBe('docker pull registry.example.com/nginx:latest')
    expect(snippet('ghcr-proxy', 'pull-alias')).toBe('docker pull registry.example.com/ghcr.io/home-assistant/home-assistant:stable')
    expect(snippet('ghcr-proxy', 'mirror')).toBe('')
  })

  it('gives yum repos a .repo file with gpgcheck on', () => {
    const guide = setupGuide(byName('rocky'))
    const repoFile = guide.clients[0]!.snippets[0]!
    expect(repoFile.filename).toBe('/etc/yum.repos.d/rocky.repo')
    expect(repoFile.content).toContain('baseurl=https://registry.example.com/repository/rocky/$releasever/BaseOS/$basearch/os/')
    expect(repoFile.content).toContain('gpgcheck=1')
    expect(snippet('oracle', 'repo')).toContain('/repository/oracle/repo/OracleLinux/OL9/baseos/latest/$basearch/')
  })

  it('serves apt suites per archive', () => {
    expect(snippet('ubuntu', 'deb822')).toContain('Suites: resolute resolute-updates resolute-backports')
    expect(snippet('debian-security', 'deb822')).toContain('Suites: trixie-security')
    expect(snippet('raspberrypi', 'deb822')).toContain('Suites: trixie\n')
  })
})

describe('mock Maven repositories', () => {
  const maven = (type: 'proxy' | 'hosted', policy = 'release') =>
    ({ ...byName('npm'), name: `maven-${type}`, format: 'maven', type, format_options: { version_policy: policy } }) as unknown as ReturnType<typeof byName>

  it('lists artifacts, versions newest first and a version folder', () => {
    const repo = maven('hosted')
    const artifacts = mavenArtifactsFor(repo)
    expect(artifacts[0]).toMatchObject({ group_id: 'com.example.platform', artifact_id: 'billing-service', latest: '1.5.0-SNAPSHOT', version_count: 3 })
    const versions = mavenVersionsFor(repo, 'com.example.platform', 'billing-service')!
    expect(versions.map((v) => v.version)).toEqual(['1.5.0-SNAPSHOT', '1.4.1', '1.4.0'])
    expect(versions[0]!.snapshot).toBe(true)
    const files = mavenListing(repo, versions[1]!.path)!
    expect(files.map((f) => f.name)).toContain('billing-service-1.4.1.jar')
    expect(mavenVersionsFor(repo, 'org.none', 'none')).toBeNull()
    expect(mavenListing(repo, 'org/none')).toBeNull()
  })

  it('gives hosted repositories deploy snippets matching their version policy', () => {
    const ids = (repo: ReturnType<typeof byName>) => setupGuide(repo).clients.flatMap((c) => c.snippets).map((s) => s.id)
    expect(ids(maven('proxy'))).toContain('settings-mirror')
    expect(ids(maven('proxy'))).not.toContain('pom-distribution')
    const pom = setupGuide(maven('hosted', 'snapshot')).clients[0]!.snippets.find((s) => s.id === 'pom-distribution')!
    expect(pom.content).toContain('<snapshotRepository>')
  })
})

describe('mock dnf runs', () => {
  it('never fetches new content through the blocked epel upstream', () => {
    for (let i = 0; i < 20; i++) {
      const plan = dnfRun('epel')
      expect(plan.specs.every((s) => s.cache !== 'FETCHING')).toBe(true)
    }
  })
})
