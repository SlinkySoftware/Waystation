// SPDX-License-Identifier: GPL-3.0-or-later
// M2 UI completion (T-U01, T-U04, T-U05): gauge chart, density, referrer kinds, the create flow's types per format
// against the server's format descriptors.
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { referrerKind } from './browse/referrers'
import GaugeChart from './charts/GaugeChart.vue'
import { GROUP_FORMATS, HOSTED_FORMATS } from './repositories/form'
import { useThemeStore } from '@/stores/theme'

const saved = vi.hoisted(() => vi.fn(async () => ({ data: {}, response: new Response() })))
vi.mock('@/api/client', () => ({ api: { PUT: saved }, call: async (p: Promise<unknown>) => p }))

describe('GaugeChart', () => {
  it('is a labelled meter whose reading is text, not only the arc', () => {
    const w = mount(GaugeChart, { props: { value: 142, max: 200, label: 'Docker Hub pulls left', caption: 'of 200', valueText: '142 of 200 pulls left' } })
    const meter = w.find('[role=meter]')
    expect(meter.attributes()).toMatchObject({ 'aria-label': 'Docker Hub pulls left', 'aria-valuenow': '142', 'aria-valuemin': '0', 'aria-valuemax': '200', 'aria-valuetext': '142 of 200 pulls left' })
    expect(w.text()).toContain('142')
    expect(w.text()).toContain('of 200')
    expect(w.find('svg').attributes('aria-hidden')).toBe('true')
  })

  it('clamps out-of-range values and draws marks', () => {
    const w = mount(GaugeChart, { props: { value: 250, max: 200, label: 'x', marks: [0.8, 2] } })
    expect(w.find('[role=meter]').attributes('aria-valuenow')).toBe('200')
    expect(w.findAll('.gauge__mark')).toHaveLength(1)
    const empty = mount(GaugeChart, { props: { value: 0, max: 0, label: 'x' } })
    expect(empty.find('.gauge__fill').exists()).toBe(false)
  })
})

describe('density', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    document.body.className = ''
  })

  it('applies compact density as a body class and keeps it for the next visit', async () => {
    const theme = useThemeStore()
    theme.init()
    expect(document.body.classList.contains('ws-compact')).toBe(false)
    await theme.save({ density: 'compact' })
    expect(document.body.classList.contains('ws-compact')).toBe(true)
    expect(localStorage.getItem('ws.density')).toBe('compact')
    expect(saved).toHaveBeenCalledWith('/auth/me/preferences', { body: { theme: 'system', density: 'compact' } })
    theme.adopt({ theme: 'light', density: 'comfortable' })
    expect(document.body.classList.contains('ws-compact')).toBe(false)
  })
})

describe('referrer kinds', () => {
  it('names signatures, SBOMs and attestations from the artifact type', () => {
    expect(referrerKind('application/vnd.dev.cosign.artifact.sig.v1+json')).toBe('signature')
    expect(referrerKind('application/vnd.cncf.notary.signature')).toBe('signature')
    expect(referrerKind('application/spdx+json')).toBe('sbom')
    expect(referrerKind('application/vnd.cyclonedx+json')).toBe('sbom')
    expect(referrerKind('application/vnd.in-toto+json')).toBe('attestation')
    expect(referrerKind('application/vnd.example.thing')).toBe('artifact')
    expect(referrerKind(null)).toBe('artifact')
  })
})

describe('create flow types', () => {
  it('offers hosted and group exactly for the formats whose server descriptor serves them', () => {
    const formats = join(__dirname, '../../../Server/waystation_server/formats')
    const served: Record<string, string[]> = {}
    for (const dir of readdirSync(formats, { withFileTypes: true })) {
      if (!dir.isDirectory()) continue
      let source: string
      try {
        source = readFileSync(join(formats, dir.name, 'descriptor.py'), 'utf8')
      } catch {
        continue
      }
      const types = /types=frozenset\(\{([^}]*)\}\)/.exec(source)?.[1]
      const name = /name='([a-z]+)'/.exec(source.slice(source.indexOf('DESCRIPTOR = FormatDescriptor(')))?.[1]
      if (types && name) served[name] = [...types.matchAll(/RepoType\.([A-Z]+)/g)].map((m) => m[1]!.toLowerCase())
    }
    expect(Object.keys(served).length).toBeGreaterThan(8)
    const hosted = Object.keys(served).filter((f) => served[f]!.includes('hosted')).sort()
    const group = Object.keys(served).filter((f) => served[f]!.includes('group')).sort()
    expect([...HOSTED_FORMATS].sort()).toEqual(hosted)
    expect([...GROUP_FORMATS].sort()).toEqual(group)
  })
})
