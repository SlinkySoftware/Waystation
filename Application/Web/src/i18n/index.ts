// SPDX-License-Identifier: GPL-3.0-or-later
// i18n-ready strings (UI_DESIGN §7): every user-facing string goes through t() with the English catalogue.
// Adding a locale = another catalogue with the same shape and a switch here.
import { en } from './en'

type Catalogue = typeof en
type Leaves<T, P extends string = ''> = {
  [K in keyof T & string]: T[K] extends string ? `${P}${K}` : Leaves<T[K], `${P}${K}.`>
}[keyof T & string]

export type MessageKey = Leaves<Catalogue>
export type MessageParams = Record<string, string | number>

function lookup(key: string): string | undefined {
  let node: unknown = en
  for (const part of key.split('.')) {
    if (node && typeof node === 'object' && part in node) node = (node as Record<string, unknown>)[part]
    else return undefined
  }
  return typeof node === 'string' ? node : undefined
}

/** Translate `key`, replacing `{name}` placeholders. Plurals: "one|other" picked by `params.count`. */
export function t(key: MessageKey, params?: MessageParams): string {
  let text = lookup(key) ?? key
  if (text.includes('|') && params && typeof params.count === 'number') {
    const [one, other] = text.split('|')
    text = (params.count === 1 ? one : other) ?? text
  }
  if (params) text = text.replace(/\{(\w+)\}/g, (match, name: string) => (name in params ? String(params[name]) : match))
  return text
}
