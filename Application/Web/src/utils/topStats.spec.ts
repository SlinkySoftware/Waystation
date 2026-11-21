// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { artifactLocation, growthDelta, growthSeries } from './topStats'

describe('artifactLocation', () => {
  it('opens the format-aware browse views', () => {
    expect(artifactLocation({ repository: 'dockerhub-proxy', format: 'docker', name: 'library/nginx', version: '1.29.3', path: null })).toEqual({
      name: 'repository',
      params: { name: 'dockerhub-proxy', tab: 'browse' },
      query: { image: 'library/nginx', ref: '1.29.3' },
    })
    expect(artifactLocation({ repository: 'npm', format: 'npm', name: 'lodash', version: '4.17.21', path: 'lodash/-/lodash-4.17.21.tgz' })).toMatchObject({ query: { package: 'lodash' } })
    expect(artifactLocation({ repository: 'rocky', format: 'yum', name: 'bash', version: '5.2', path: 'x.rpm' })).toMatchObject({ query: { view: 'packages', package: 'bash' } })
  })

  it('opens the folder of a raw file', () => {
    expect(artifactLocation({ repository: 'raw-releases', format: 'raw', name: 'jq-linux-amd64', version: null, path: 'jqlang/jq/releases/download/jq-1.8.1/jq-linux-amd64' })).toMatchObject({
      query: { path: 'jqlang/jq/releases/download/jq-1.8.1' },
    })
    expect(artifactLocation({ repository: 'raw-releases', format: 'raw', name: 'top.txt', version: null, path: 'top.txt' })).toMatchObject({ query: {} })
  })
})

describe('growthSeries', () => {
  it('is empty without snapshots', () => {
    expect(growthSeries({ used_bytes: [null, null], stored_bytes: [null, null] })).toEqual({ used: [0, 0], saved: [0, 0], empty: true })
  })

  it('carries the last snapshot over gaps and splits stored into used + saved', () => {
    const s = growthSeries({ used_bytes: [null, 100, null, 150], stored_bytes: [null, 120, null, 140] })
    expect(s.used).toEqual([0, 100, 100, 150])
    expect(s.saved).toEqual([0, 20, 20, 0])
    expect(s.empty).toBe(false)
  })
})

describe('growthDelta', () => {
  it('needs two snapshots', () => {
    expect(growthDelta([null, 5])).toBeNull()
    expect(growthDelta([null, 5, null, 12])).toBe(7)
  })
})
