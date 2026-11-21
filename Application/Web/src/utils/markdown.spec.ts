// SPDX-License-Identifier: GPL-3.0-or-later
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import MarkdownView from '@/components/MarkdownView.vue'

import { safeHref } from './markdown'

const render = (source: string) => mount(MarkdownView, { props: { source } })

describe('safe Markdown (READMEs)', () => {
  it('renders the common elements', () => {
    const w = render('# Title\n\nSome *em* and **strong** and `code`.\n\n- a\n- b\n\n1. one\n2. two\n\n> quote\n\n```js\nconst x = 1\n```\n\n---')
    expect(w.find('h3').text()).toBe('Title') // h1 shifted below the page headings
    expect(w.find('em').text()).toBe('em')
    expect(w.find('strong').text()).toBe('strong')
    expect(w.find('p code').text()).toBe('code')
    expect(w.findAll('ul li').map((li) => li.text())).toEqual(['a', 'b'])
    expect(w.findAll('ol li')).toHaveLength(2)
    expect(w.find('blockquote').text()).toBe('quote')
    expect(w.find('pre code').text()).toBe('const x = 1')
    expect(w.find('pre code').attributes('data-lang')).toBe('js')
    expect(w.find('hr').exists()).toBe(true)
    // tight list items carry no <p>
    expect(w.find('li p').exists()).toBe(false)
  })

  it('renders GFM tables with alignment', () => {
    const w = render('| a | b |\n| --- | ---: |\n| 1 | 2 |')
    expect(w.find('.md-table table').exists()).toBe(true)
    expect(w.findAll('th').map((th) => th.text())).toEqual(['a', 'b'])
    expect(w.findAll('td')[1]!.classes()).toContain('md-align-right')
  })

  it('never turns raw HTML into elements', () => {
    const w = render('<script>alert(1)</script>\n\n<img src=x onerror=alert(2)> <b>bold</b>\n\n<div onclick="x()">div</div>')
    expect(w.find('script').exists()).toBe(false)
    expect(w.find('img').exists()).toBe(false)
    expect(w.find('b').exists()).toBe(false)
    expect(w.find('[onclick]').exists()).toBe(false)
    expect(w.text()).toContain('<script>alert(1)</script>')
    expect(w.text()).toContain('<b>bold</b>')
  })

  it('keeps only safe links, opened without a referrer', () => {
    const w = render('[ok](https://example.com/a) [mail](mailto:dev@example.com) [js](javascript:alert(1)) [data](data:text/html,x) [rel](./docs/a.md) [anchor](#usage)')
    const links = w.findAll('a')
    expect(links.map((a) => a.attributes('href'))).toEqual(['https://example.com/a', 'mailto:dev@example.com', '#usage'])
    expect(links[0]!.attributes('rel')).toBe('noopener noreferrer nofollow')
    expect(links[0]!.attributes('target')).toBe('_blank')
    expect(w.text()).toContain('js')
    expect(w.text()).toContain('rel')
  })

  it('does not load images: they become links named by their alt text', () => {
    const w = render('![build status](https://example.com/badge.svg) ![evil](javascript:alert(1))')
    expect(w.find('img').exists()).toBe(false)
    const link = w.find('a.md-image')
    expect(link.attributes('href')).toBe('https://example.com/badge.svg')
    expect(link.text()).toBe('[build status]')
    expect(w.text()).toContain('[evil]')
    const badge = render('[![npm](https://example.com/v.svg)](https://example.com/pkg)')
    expect(badge.findAll('a')).toHaveLength(1) // links never nest
    expect(badge.find('a').text()).toBe('[npm]')
  })

  it('accepts only http(s), mailto and in-page targets', () => {
    expect(safeHref('https://example.com')).toBe('https://example.com/')
    expect(safeHref(' http://example.com/x ')).toBe('http://example.com/x')
    expect(safeHref('#top')).toBe('#top')
    expect(safeHref('JaVaScRiPt:alert(1)')).toBeNull()
    expect(safeHref('vbscript:x')).toBeNull()
    expect(safeHref('/relative')).toBeNull()
    expect(safeHref(null)).toBeNull()
  })
})
