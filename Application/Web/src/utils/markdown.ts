// SPDX-License-Identifier: GPL-3.0-or-later
// Safe Markdown (READMEs are untrusted upstream content, CLAUDE.md "Never trust upstream content"): markdown-it
// parses with raw HTML off (any HTML in the source stays text), and the token stream is turned into Vue VNodes here,
// never into an HTML string, so nothing is ever parsed as markup by the browser. Only an allow-list of elements is
// produced; links keep http(s)/mailto/in-page targets only and open in a new tab without a referrer; images are not
// loaded (no external requests, UI_DESIGN §2): they become links named by their alt text. Headings are shifted
// down so a README never outranks the page's own headings.
import MarkdownIt, { type Token } from 'markdown-it'
import { h, type VNode, type VNodeArrayChildren } from 'vue'

const parser = new MarkdownIt({ html: false, linkify: true, typographer: false })

/** Inputs larger than this are cut (the server bounds READMEs at 256 KiB already). */
export const MAX_MARKDOWN = 256 * 1024

export function safeHref(href: string | null | undefined): string | null {
  if (!href) return null
  const value = href.trim()
  if (value.startsWith('#')) return value
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' || url.protocol === 'mailto:' ? url.href : null
  } catch {
    return null // relative links point into the package source, which is not served here
  }
}

interface Frame {
  tag: string | null
  props: Record<string, unknown>
  children: VNodeArrayChildren
}

const BLOCK_TAGS: Record<string, string> = {
  paragraph: 'p',
  bullet_list: 'ul',
  ordered_list: 'ol',
  list_item: 'li',
  blockquote: 'blockquote',
  table: 'table',
  thead: 'thead',
  tbody: 'tbody',
  tr: 'tr',
  th: 'th',
  td: 'td',
  strong: 'strong',
  em: 'em',
  s: 's',
}

/** An attribute as text (markdown-it types allow numbers too). */
function attr(token: Token, name: string): string | null {
  const value = token.attrGet(name)
  return value === null || value === undefined ? null : String(value)
}

function alignOf(token: Token): string | undefined {
  const style = attr(token, 'style') ?? ''
  const m = /text-align:\s*(left|right|center)/.exec(style)
  return m ? `md-align-${m[1]}` : undefined
}

/** VNodes for `source` (Markdown); `headingOffset` adds to every heading level (h1 → h3 with 2). */
export function renderMarkdown(source: string, headingOffset = 2): VNode[] {
  const tokens = parser.parse(source.slice(0, MAX_MARKDOWN), {})
  const root: Frame = { tag: null, props: {}, children: [] }
  const stack: Frame[] = [root]
  const top = () => stack[stack.length - 1]!

  const open = (tag: string | null, props: Record<string, unknown> = {}) => stack.push({ tag, props, children: [] })
  const close = () => {
    const frame = stack.pop()!
    const parent = top()
    if (frame.tag === null) parent.children.push(...frame.children)
    else parent.children.push(h(frame.tag, frame.props, frame.children))
  }

  const walk = (list: Token[]): void => {
    for (const token of list) {
      const base = token.type.replace(/_(open|close)$/, '')
      if (token.type === 'inline') {
        walk(token.children ?? [])
      } else if (token.type === 'text') {
        top().children.push(token.content)
      } else if (token.type === 'softbreak') {
        top().children.push('\n')
      } else if (token.type === 'hardbreak') {
        top().children.push(h('br'))
      } else if (token.type === 'code_inline') {
        top().children.push(h('code', token.content))
      } else if (token.type === 'fence' || token.type === 'code_block') {
        const lang = token.info.trim().split(/\s+/)[0] ?? ''
        top().children.push(h('pre', { class: 'md-code' }, [h('code', lang ? { 'data-lang': lang.slice(0, 32) } : {}, token.content)]))
      } else if (token.type === 'hr') {
        top().children.push(h('hr'))
      } else if (token.type === 'html_block' || token.type === 'html_inline') {
        top().children.push(token.content) // only with html: true; text either way
      } else if (token.type === 'image') {
        const href = safeHref(attr(token, 'src'))
        const alt = token.content || attr(token, 'alt') || 'image'
        // Inside a link (a badge) the alt text is enough: links never nest.
        const inLink = stack.some((f) => f.tag === 'a')
        top().children.push(href && !inLink ? h('a', { href, target: '_blank', rel: 'noopener noreferrer nofollow', class: 'md-image' }, `[${alt}]`) : `[${alt}]`)
      } else if (token.type === 'heading_open') {
        const level = Math.min(6, Number(token.tag.slice(1)) + headingOffset)
        open(`h${level}`)
      } else if (token.type === 'link_open') {
        const href = safeHref(attr(token, 'href'))
        open(href ? 'a' : 'span', href ? { href, target: href.startsWith('#') ? undefined : '_blank', rel: 'noopener noreferrer nofollow' } : {})
      } else if (token.type === 'ordered_list_open') {
        const start = Number(attr(token, 'start') ?? 1)
        open('ol', start !== 1 && Number.isFinite(start) ? { start } : {})
      } else if (token.type === 'table_open') {
        open('div', { class: 'md-table' })
        open('table')
      } else if (token.type === 'table_close') {
        close()
        close()
      } else if ((token.type === 'th_open' || token.type === 'td_open') && BLOCK_TAGS[base]) {
        open(BLOCK_TAGS[base]!, { class: alignOf(token) })
      } else if (token.nesting === 1) {
        // Tight list paragraphs are hidden by markdown-it: keep their text without a <p>.
        open(token.hidden ? null : (BLOCK_TAGS[base] ?? null))
      } else if (token.nesting === -1) {
        if (stack.length > 1) close()
      }
    }
  }
  walk(tokens)
  while (stack.length > 1) close()
  return root.children as VNode[]
}
