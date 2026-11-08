// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { formatColor, formatLabel, formatMark, LISTED_FORMATS } from './domain'

describe('format display metadata', () => {
  it('refines apt and yum marks by upstream', () => {
    expect(formatMark('apt', 'http://archive.ubuntu.com/ubuntu/')).toBe('ubuntu')
    expect(formatMark('apt', 'https://archive.raspberrypi.com/debian/')).toBe('raspberrypi')
    expect(formatMark('apt', 'https://deb.debian.org/debian/')).toBe('debian')
    expect(formatMark('yum', 'https://dl.rockylinux.org/pub/rocky/')).toBe('rockylinux')
    expect(formatMark('yum', 'https://repo.almalinux.org/almalinux/')).toBe('almalinux')
    expect(formatMark('yum', 'https://dl.fedoraproject.org/pub/epel/')).toBe('fedora')
    expect(formatMark('yum', 'https://yum.oracle.com/')).toBe('redhat')
    expect(formatMark('yum')).toBe('redhat')
  })

  it('lists yum in filters with its own label and chart colour', () => {
    expect(LISTED_FORMATS).toContain('yum')
    expect(formatLabel('yum')).toBe('yum')
    expect(formatColor('yum')).toBe('var(--chart-5)')
  })
})
