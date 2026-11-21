// SPDX-License-Identifier: GPL-3.0-or-later
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { TreeRow } from './folderTree'
import VirtualTree from './VirtualTree.vue'

function rows(count: number): TreeRow[] {
  const out: TreeRow[] = [{ type: 'node', key: '/', path: '', name: 'repo', kind: 'folder', level: 1, setsize: 1, posinset: 1, entry: null }]
  out.push({ type: 'node', key: 'dir', path: 'dir', name: 'dir', kind: 'folder', level: 2, setsize: count + 1, posinset: 1, entry: null })
  for (let i = 0; i < count; i++) out.push({ type: 'node', key: `f${i}`, path: `f${i}`, name: `f${i}`, kind: 'file', level: 2, setsize: count + 1, posinset: i + 2, entry: null })
  return out
}

describe('virtual tree', () => {
  it('renders only the rows in view, as an ARIA tree', () => {
    const w = mount(VirtualTree, { props: { rows: rows(100_000), selected: null, label: 'Files', isExpanded: () => false } })
    const tree = w.find('[role=tree]')
    expect(tree.attributes('aria-label')).toBe('Files')
    expect(tree.attributes('tabindex')).toBe('0')
    const items = w.findAll('[role=treeitem]')
    expect(items.length).toBeGreaterThan(10)
    expect(items.length).toBeLessThan(100)
    expect(items[2]!.attributes()).toMatchObject({ 'aria-level': '2', 'aria-posinset': '2', 'aria-setsize': '100001' })
    expect(items[1]!.attributes('aria-expanded')).toBe('false')
    expect(items[2]!.attributes('aria-expanded')).toBeUndefined()
  })

  it('moves with the keyboard and asks to open, close and select', async () => {
    const opened = new Set<string>()
    const w = mount(VirtualTree, { props: { rows: rows(5), selected: null, label: 'Files', isExpanded: (p: string) => p === '' || opened.has(p) } })
    const tree = w.find('[role=tree]')
    await tree.trigger('focus')
    expect(tree.attributes('aria-activedescendant')).toMatch(/^vt-/)
    await tree.trigger('keydown', { key: 'ArrowDown' })
    await tree.trigger('keydown', { key: 'ArrowRight' })
    expect(w.emitted('toggle')?.[0]).toEqual(['dir', true])
    opened.add('dir')
    await tree.trigger('keydown', { key: 'ArrowLeft' })
    expect(w.emitted('toggle')?.[1]).toEqual(['dir', false])
    await tree.trigger('keydown', { key: 'End' })
    await tree.trigger('keydown', { key: 'Enter' })
    expect(w.emitted('select')?.[0]).toEqual(['f4'])
    await tree.trigger('keydown', { key: 'Home' })
    await tree.trigger('keydown', { key: ' ' })
    expect(w.emitted('select')?.[1]).toEqual([''])
    const active = tree.attributes('aria-activedescendant')!
    expect(w.find(`#${active}`).classes()).toContain('vtree__row--active')
  })

  it('asks for the next page when a "more" row is in view', () => {
    const list: TreeRow[] = [...rows(3), { type: 'more', key: '/…more', path: '', level: 2, loading: false, failed: false }]
    const w = mount(VirtualTree, { props: { rows: list, selected: null, label: 'Files', isExpanded: () => true } })
    expect(w.emitted('more')?.[0]).toEqual([''])
  })
})
