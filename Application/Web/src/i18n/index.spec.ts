// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { t } from './index'

describe('t()', () => {
  it('looks up nested keys', () => {
    expect(t('nav.dashboard')).toBe('Dashboard')
  })

  it('interpolates and pluralises', () => {
    expect(t('live.activeCount', { count: 1 })).toBe('1 active transfer')
    expect(t('live.activeCount', { count: 3 })).toBe('3 active transfers')
  })

  it('falls back to the key when missing', () => {
    // @ts-expect-error: unknown key on purpose
    expect(t('nope.missing')).toBe('nope.missing')
  })
})
