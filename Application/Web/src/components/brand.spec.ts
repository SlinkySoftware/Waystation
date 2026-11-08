// SPDX-License-Identifier: GPL-3.0-or-later
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import { MARK, MARK_SMALL, WORDMARK } from '@/assets/brand/geometry'

import BrandMark from './BrandMark.vue'
import EmptyIllustration from './EmptyIllustration.vue'
import EmptyState from './EmptyState.vue'
import { ILLUSTRATIONS } from './illustrations'

// Vitest runs from Application/Web.
const brandFile = (name: string): string => readFileSync(join(process.cwd(), 'src/assets/brand', name), 'utf8')
const publicFile = (name: string): string => readFileSync(join(process.cwd(), 'public', name), 'utf8')

describe('BrandMark', () => {
  it('draws the shared geometry, switching to the 16 px glyph below 20 px', () => {
    const big = mount(BrandMark, { props: { size: 28 } })
    expect(big.findAll('path').map((p) => p.attributes('d'))).toEqual([MARK.marker, MARK.slab, MARK.layers.join('')])
    const small = mount(BrandMark, { props: { size: 16 } })
    expect(small.findAll('path').map((p) => p.attributes('d'))).toEqual([MARK_SMALL.marker, MARK_SMALL.slab, MARK_SMALL.layers.join('')])
  })

  it('is decorative by default and a labelled image when given a label', () => {
    const plain = mount(BrandMark)
    expect(plain.attributes('aria-hidden')).toBe('true')
    expect(plain.attributes('role')).toBeUndefined()
    const labelled = mount(BrandMark, { props: { label: 'Waystation' } })
    expect(labelled.attributes('role')).toBe('img')
    expect(labelled.attributes('aria-label')).toBe('Waystation')
    expect(labelled.attributes('aria-hidden')).toBeUndefined()
  })

  it('cuts the glyph out of the mono mark with a mask id unique per instance', () => {
    const host = mount({ components: { BrandMark }, template: '<div><BrandMark variant="mono" /><BrandMark variant="mono" /></div>' })
    const ids = host.findAll('mask').map((m) => m.attributes('id'))
    expect(ids).toHaveLength(2)
    expect(new Set(ids).size).toBe(2)
    for (const id of ids) expect(host.find(`path[mask="url(#${id})"]`).exists()).toBe(true)
  })
})

describe('brand assets', () => {
  it('committed masters and favicon match the geometry (make brand regenerates them)', () => {
    expect(brandFile('mark.svg')).toContain(`d="${MARK.marker}"`)
    expect(brandFile('mark.svg')).toContain(`d="${MARK.slab}"`)
    expect(brandFile('mark-small.svg')).toContain(`d="${MARK_SMALL.layers.join('')}"`)
    expect(brandFile('wordmark.svg')).toContain(`d="${WORDMARK.paths[0]}"`)
    expect(publicFile('favicon.svg')).toContain('prefers-color-scheme:dark')
    expect(publicFile('favicon.svg')).toContain(`d="${MARK_SMALL.slab}"`)
  })

  it('the web manifest lists the icon set, including a maskable icon, with relative URLs', () => {
    const manifest = JSON.parse(publicFile('manifest.webmanifest')) as { start_url: string; icons: { src: string; purpose?: string }[] }
    expect(manifest.start_url).toBe('./')
    expect(manifest.icons.some((i) => i.purpose === 'maskable')).toBe(true)
    for (const icon of manifest.icons) expect(icon.src).not.toMatch(/^\/|:\/\//)
  })
})

describe('empty-state illustrations', () => {
  it('each illustration draws something different and is painted only through theme variables', () => {
    const bodies = new Set<string>()
    for (const name of ILLUSTRATIONS) {
      const w = mount(EmptyIllustration, { props: { name } })
      expect(w.attributes('aria-hidden')).toBe('true')
      expect(w.attributes('data-illustration')).toBe(name)
      expect(w.html()).not.toMatch(/(fill|stroke)="(?!none)[^"]+"/)
      expect(w.html()).not.toMatch(/#[0-9a-f]{3,8}\b/i)
      bodies.add(w.html().replace(/data-illustration="[^"]+"/, ''))
    }
    expect(bodies.size).toBe(ILLUSTRATIONS.length)
  })

  it('EmptyState shows the illustration when given one, the icon tile otherwise', () => {
    const illustrated = mount(EmptyState, { props: { title: 'No repositories yet', illustration: 'repositories' } })
    expect(illustrated.find('[data-illustration="repositories"]').exists()).toBe(true)
    expect(illustrated.find('.empty-state__art').exists()).toBe(false)
    expect(illustrated.text()).toContain('No repositories yet')

    const plain = mount(EmptyState, { props: { title: 'Nothing here' } })
    expect(plain.find('svg.illo').exists()).toBe(false)
    expect(plain.find('.empty-state__art .q-icon').exists()).toBe(true)
  })
})
