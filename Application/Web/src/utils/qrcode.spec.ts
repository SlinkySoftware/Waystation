// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { alignmentPositions, dataCodewords, encodeQr, formatBits, penalty, qrPath, rsDivisor, rsRemainder, versionBits } from './qrcode'

describe('QR encoder building blocks', () => {
  it('computes Reed-Solomon codewords like ISO/IEC 18004 Annex I (1-M, "01234567")', () => {
    const data = [0x10, 0x20, 0x0c, 0x56, 0x61, 0x80, 0xec, 0x11, 0xec, 0x11, 0xec, 0x11, 0xec, 0x11, 0xec, 0x11]
    expect(rsRemainder(data, rsDivisor(10))).toEqual([0xa5, 0x24, 0xd4, 0xc1, 0xed, 0x36, 0xc7, 0x87, 0x2c, 0x55])
  })

  it('encodes format and version information with their BCH codes', () => {
    expect(formatBits(0).toString(2).padStart(15, '0')).toBe('101010000010010') // level M, mask 0
    expect(formatBits(4).toString(2).padStart(15, '0')).toBe('100010111111001') // level M, mask 4
    expect(formatBits(7).toString(2).padStart(15, '0')).toBe('100101010100000') // level M, mask 7
    expect(versionBits(7).toString(16)).toBe('7c94')
    expect(versionBits(40).toString(16)).toBe('28c69')
  })

  it('knows the level M capacities and alignment pattern positions', () => {
    expect([1, 2, 3, 4, 5, 6, 7, 10, 40].map(dataCodewords)).toEqual([16, 28, 44, 64, 86, 108, 124, 216, 2334])
    expect(alignmentPositions(1)).toEqual([])
    expect(alignmentPositions(2)).toEqual([6, 18])
    expect(alignmentPositions(7)).toEqual([6, 22, 38])
    expect(alignmentPositions(32)).toEqual([6, 34, 60, 86, 112, 138])
  })
})

describe('encodeQr', () => {
  const uri = 'otpauth://totp/Waystation:ana?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=Waystation&algorithm=SHA1&digits=6&period=30'

  it('picks the smallest version that fits and draws the finder patterns', () => {
    const qr = encodeQr(uri)
    expect(qr.version).toBe(7)
    expect(qr.size).toBe(45)
    const finderRow = [true, true, true, true, true, true, true]
    for (const [x, y] of [
      [0, 0],
      [qr.size - 7, 0],
      [0, qr.size - 7],
    ] as const) {
      expect(qr.modules[y]!.slice(x, x + 7)).toEqual(finderRow)
      expect(qr.modules[y + 1]!.slice(x, x + 7)).toEqual([true, false, false, false, false, false, true])
    }
    expect(qr.modules[qr.size - 8]![8]).toBe(true) // the dark module
    expect(encodeQr('a').version).toBe(1)
  })

  it('chooses the mask with the lowest penalty and writes it into the format information', () => {
    const qr = encodeQr(uri)
    const bits = formatBits(qr.mask)
    const read = [0, 1, 2, 3, 4, 5].map((y) => qr.modules[y]![8])
    expect(read).toEqual([0, 1, 2, 3, 4, 5].map((i) => ((bits >>> i) & 1) === 1))
    expect(penalty(qr.modules)).toBeGreaterThan(0)
  })

  it('is deterministic and renders to an SVG path with a quiet zone', () => {
    const a = encodeQr('hello')
    expect(encodeQr('hello')).toEqual(a)
    const path = qrPath(a)
    expect(path.startsWith('M4 4h1v1h-1z')).toBe(true)
    expect(path.split('z').length - 1).toBe(a.modules.flat().filter(Boolean).length)
  })

  it('refuses text that cannot fit', () => {
    expect(() => encodeQr('x'.repeat(3000))).toThrow('too long')
  })
})
