// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import {
  etaSeconds,
  formatBytes,
  formatCount,
  formatDuration,
  formatLatency,
  formatPercent,
  formatRate,
  relativeTime,
  shortDigest,
  truncateMiddle,
} from './format'

describe('formatBytes (binary units, 3 significant digits)', () => {
  it.each([
    [0, '0 B'],
    [512, '512 B'],
    [1023, '1023 B'],
    [1024, '1.00 KiB'],
    [1536, '1.50 KiB'],
    [10 * 1024 + 300, '10.3 KiB'],
    [123.4 * 1024 * 1024, '123 MiB'],
    [1023.9 * 1024, '1.00 MiB'],
    [5 * 1024 ** 3, '5.00 GiB'],
    [2.5 * 1024 ** 4, '2.50 TiB'],
  ])('%d -> %s', (bytes, text) => expect(formatBytes(bytes)).toBe(text))

  it('handles missing values', () => {
    expect(formatBytes(null)).toBe('–')
    expect(formatBytes(undefined)).toBe('–')
    expect(formatBytes(Number.NaN)).toBe('–')
  })
})

describe('formatRate (SI units)', () => {
  it.each([
    [0, '0 B/s'],
    [950, '950 B/s'],
    [12_500_000, '12.5 MB/s'],
    [999_700, '1.00 MB/s'],
    [1_100_000_000, '1.10 GB/s'],
  ])('%d -> %s', (rate, text) => expect(formatRate(rate)).toBe(text))
})

describe('other formatters', () => {
  it('formats counts compactly above 9,999', () => {
    expect(formatCount(950)).toBe('950')
    expect(formatCount(9999)).toBe('9,999')
    expect(formatCount(34_567)).toBe('34.6K')
    expect(formatCount(1_234_567)).toBe('1.23M')
  })

  it('formats percentages', () => {
    expect(formatPercent(0.874)).toBe('87.4%')
    expect(formatPercent(1)).toBe('100%')
    expect(formatPercent(0.99971)).toBe('>99.9%')
    expect(formatPercent(null)).toBe('–')
  })

  it('formats durations', () => {
    expect(formatDuration(0.8)).toBe('0.8 s')
    expect(formatDuration(42)).toBe('42 s')
    expect(formatDuration(185)).toBe('3m 05s')
    expect(formatDuration(7800)).toBe('2h 10m')
    expect(formatDuration(3 * 86400 + 4 * 3600)).toBe('3d 4h')
    expect(formatDuration(-1)).toBe('–')
  })

  it('formats latency', () => {
    expect(formatLatency(4.2)).toBe('4 ms')
    expect(formatLatency(1234)).toBe('1.2 s')
  })

  it('computes ETA only when it is known', () => {
    expect(etaSeconds(50, 150, 10)).toBe(10)
    expect(etaSeconds(50, null, 10)).toBeNull()
    expect(etaSeconds(50, 150, 0)).toBeNull()
  })

  it('formats relative times', () => {
    const now = Date.parse('2026-11-08T12:00:00Z')
    expect(relativeTime('2026-11-08T11:59:55Z', now)).toBe('just now')
    expect(relativeTime('2026-11-08T11:55:00Z', now)).toBe('5 min. ago')
    expect(relativeTime('2026-11-08T09:00:00Z', now)).toBe('3 hr. ago')
    expect(relativeTime(null, now)).toBe('–')
  })

  it('truncates in the middle', () => {
    expect(truncateMiddle('short', 10)).toBe('short')
    const out = truncateMiddle('library/nginx@sha256:3f1a2b3c4d5e6f', 20)
    expect(out).toHaveLength(20)
    expect(out.startsWith('library/ngin')).toBe(true)
    expect(out).toContain('…')
    expect(out.endsWith('5e6f')).toBe(true)
  })

  it('shortens digests', () => {
    expect(shortDigest('sha256:0123456789abcdef0123')).toBe('sha256:0123456789ab')
    expect(shortDigest(null)).toBe('–')
  })
})
