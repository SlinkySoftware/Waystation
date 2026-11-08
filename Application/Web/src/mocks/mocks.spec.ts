// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { seedRepositories } from './fixtures/repositories'
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

describe('mock dnf runs', () => {
  it('never fetches new content through the blocked epel upstream', () => {
    for (let i = 0; i < 20; i++) {
      const plan = dnfRun('epel')
      expect(plan.specs.every((s) => s.cache !== 'FETCHING')).toBe(true)
    }
  })
})
