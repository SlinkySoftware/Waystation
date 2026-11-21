// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import type { FolderEntry } from '@/api/types'

import { ancestorsOf, parentOf, type TreeRow, useFolderTree } from './folderTree'

const folder = (path: string, child_count: number | null = null): FolderEntry => ({ name: path.split('/').pop()!, path, kind: 'folder', child_count })
const file = (path: string): FolderEntry => ({ name: path.split('/').pop()!, path, kind: 'file', size: 1 })

/** A fake listing: `big` holds 1,200 files in pages of 500. */
const TREE: Record<string, FolderEntry[]> = {
  '': [folder('a', 2), folder('big', 1200), file('readme.txt')],
  a: [folder('a/b', 1), file('a/x.bin')],
  'a/b': [file('a/b/deep.tar.gz')],
  big: Array.from({ length: 1200 }, (_, i) => file(`big/f-${String(i).padStart(4, '0')}`)),
}

function fake() {
  return vi.fn(async (path: string, cursor: string | null) => {
    const all = TREE[path] ?? []
    const offset = cursor ? Number(cursor) : 0
    const next = offset + 500 < all.length ? String(offset + 500) : null
    return { entries: all.slice(offset, offset + 500), next_cursor: next }
  })
}

const names = (rows: TreeRow[]) => rows.map((r) => (r.type === 'node' ? `${'  '.repeat(r.level - 1)}${r.name}` : `${'  '.repeat(r.level - 1)}…`))

describe('folder tree', () => {
  it('works out parents and ancestors', () => {
    expect(parentOf('a/b/c')).toBe('a/b')
    expect(parentOf('a')).toBe('')
    expect(ancestorsOf('a/b/c')).toEqual(['', 'a', 'a/b'])
    expect(ancestorsOf('')).toEqual([])
  })

  it('lists the root, expands folders lazily and collapses them', async () => {
    const fetch = fake()
    const tree = useFolderTree(fetch, ref('files'))
    await tree.expand('')
    expect(names(tree.rows.value)).toEqual(['files', '  a', '  big', '  readme.txt'])
    await tree.expand('a')
    expect(names(tree.rows.value)).toEqual(['files', '  a', '    b', '    x.bin', '  big', '  readme.txt'])
    const b = tree.rows.value[2]!
    expect(b.type === 'node' && [b.level, b.posinset, b.setsize]).toEqual([3, 1, 2])
    tree.collapse('a')
    expect(names(tree.rows.value)).toHaveLength(4)
    await tree.expand('a') // cached: no second request
    expect(fetch.mock.calls.filter(([p]) => p === 'a')).toHaveLength(1)
  })

  it('pages a large folder with a "more" row and shares concurrent loads', async () => {
    const fetch = fake()
    const tree = useFolderTree(fetch, ref('files'))
    await tree.expand('')
    await tree.expand('big')
    let rows = tree.rows.value
    expect(rows.filter((r) => r.type === 'node' && r.path.startsWith('big/'))).toHaveLength(500)
    expect(rows.some((r) => r.type === 'more' && r.path === 'big')).toBe(true)
    const node = rows.find((r) => r.type === 'node' && r.path === 'big/f-0000')!
    expect(node.type === 'node' && node.setsize).toBe(501) // 500 + the "more" row
    await Promise.all([tree.load('big', true), tree.load('big', true)])
    expect(fetch.mock.calls.filter(([p]) => p === 'big')).toHaveLength(2)
    await tree.load('big', true)
    rows = tree.rows.value
    expect(rows.filter((r) => r.type === 'node' && r.path.startsWith('big/'))).toHaveLength(1200)
    expect(rows.some((r) => r.type === 'more')).toBe(false)
  })

  it('reveals a deep path, paging its folder until the entry shows', async () => {
    const tree = useFolderTree(fake(), ref('files'))
    expect(await tree.reveal('a/b/deep.tar.gz')).toMatchObject({ kind: 'file', path: 'a/b/deep.tar.gz' })
    expect(tree.isExpanded('a') && tree.isExpanded('a/b')).toBe(true)
    expect(await tree.reveal('big/f-1100')).toMatchObject({ path: 'big/f-1100' })
    expect(await tree.reveal('a/missing')).toBeNull()
  })

  it('keeps a failed folder retryable', async () => {
    let fail = true
    const tree = useFolderTree(async () => {
      if (fail) throw new Error('boom')
      return { entries: [file('x')], next_cursor: null }
    }, ref('r'))
    await tree.expand('')
    expect(tree.state('').error).toBeTruthy()
    expect(tree.rows.value.at(-1)).toMatchObject({ type: 'more', failed: true })
    fail = false
    await tree.load('')
    expect(names(tree.rows.value)).toEqual(['r', '  x'])
  })
})
