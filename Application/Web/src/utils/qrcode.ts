// SPDX-License-Identifier: GPL-3.0-or-later
// A small QR code encoder (ISO/IEC 18004, model 2): byte mode, error correction level M, versions 1-40, the mask with
// the lowest penalty. Used to show the TOTP enrolment URI as a QR code without sending the secret anywhere: the
// matrix is drawn as an SVG path in the browser (components/account/QrCode.vue). No external requests, no
// dependency.

/** Error correction codewords per block, level M, by version (index 0 unused). */
const ECC_PER_BLOCK = [
  -1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28,
  28, 28, 28, 28, 28, 28, 28, 28, 28, 28,
]
/** Error correction blocks, level M, by version (index 0 unused). */
const BLOCKS = [
  -1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35,
  37, 38, 40, 43, 45, 47, 49,
]
const FORMAT_LEVEL_M = 0 // the two error-correction-level bits of the format information for level M

export interface QrMatrix {
  version: number
  size: number
  mask: number
  /** `modules[y][x]`: true = dark. */
  modules: boolean[][]
}

// ---------------------------------------------------------------------------- GF(256) and Reed-Solomon
function gfMultiply(x: number, y: number): number {
  let z = 0
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d)
    z ^= ((y >>> i) & 1) * x
  }
  return z & 0xff
}

/** The generator polynomial of the given degree (coefficients from the highest power down, leading 1 omitted). */
export function rsDivisor(degree: number): number[] {
  const result: number[] = new Array<number>(degree - 1).fill(0)
  result.push(1)
  let root = 1
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < result.length; j++) {
      result[j] = gfMultiply(result[j]!, root)
      if (j + 1 < result.length) result[j]! ^= result[j + 1]!
    }
    root = gfMultiply(root, 0x02)
  }
  return result
}

/** The error correction codewords of `data` for `divisor`. */
export function rsRemainder(data: readonly number[], divisor: readonly number[]): number[] {
  const result: number[] = divisor.map(() => 0)
  for (const byte of data) {
    const factor = byte ^ (result.shift() ?? 0)
    result.push(0)
    divisor.forEach((coefficient, i) => {
      result[i]! ^= gfMultiply(coefficient, factor)
    })
  }
  return result
}

// ---------------------------------------------------------------------------- capacities and layout
function rawDataModules(version: number): number {
  let result = (16 * version + 128) * version + 64
  if (version >= 2) {
    const align = Math.floor(version / 7) + 2
    result -= (25 * align - 10) * align - 55
    if (version >= 7) result -= 36
  }
  return result
}

export function dataCodewords(version: number): number {
  return Math.floor(rawDataModules(version) / 8) - ECC_PER_BLOCK[version]! * BLOCKS[version]!
}

export function alignmentPositions(version: number): number[] {
  if (version === 1) return []
  const count = Math.floor(version / 7) + 2
  const step = Math.floor((version * 8 + count * 3 + 5) / (count * 4 - 4)) * 2
  const size = version * 4 + 17
  const result = [6]
  for (let pos = size - 7; result.length < count; pos -= step) result.splice(1, 0, pos)
  return result
}

/** The 15 format bits (level M, `mask`) with their BCH code, masked as the standard requires. */
export function formatBits(mask: number): number {
  const data = (FORMAT_LEVEL_M << 3) | mask
  let rem = data
  for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537)
  return ((data << 10) | rem) ^ 0x5412
}

/** The 18 version bits (versions 7 and up) with their BCH code. */
export function versionBits(version: number): number {
  let rem = version
  for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25)
  return (version << 12) | rem
}

function bit(value: number, i: number): boolean {
  return ((value >>> i) & 1) !== 0
}

// ---------------------------------------------------------------------------- data codewords
function encodeData(bytes: Uint8Array, version: number): number[] {
  const capacity = dataCodewords(version) * 8
  const bits: number[] = []
  const append = (value: number, length: number): void => {
    for (let i = length - 1; i >= 0; i--) bits.push((value >>> i) & 1)
  }
  append(0b0100, 4) // byte mode
  append(bytes.length, version <= 9 ? 8 : 16)
  for (const b of bytes) append(b, 8)
  append(0, Math.min(4, capacity - bits.length)) // terminator
  append(0, (8 - (bits.length % 8)) % 8)
  const out: number[] = []
  for (let i = 0; i < bits.length; i += 8) out.push(bits.slice(i, i + 8).reduce((acc, b) => (acc << 1) | b, 0))
  for (let pad = 0xec; out.length < capacity / 8; pad ^= 0xec ^ 0x11) out.push(pad)
  return out
}

function interleave(data: number[], version: number): number[] {
  const blocks = BLOCKS[version]!
  const eccLen = ECC_PER_BLOCK[version]!
  const raw = Math.floor(rawDataModules(version) / 8)
  const shortBlocks = blocks - (raw % blocks)
  const shortLen = Math.floor(raw / blocks)
  const divisor = rsDivisor(eccLen)
  const all: number[][] = []
  for (let i = 0, k = 0; i < blocks; i++) {
    const chunk = data.slice(k, k + shortLen - eccLen + (i < shortBlocks ? 0 : 1))
    k += chunk.length
    const ecc = rsRemainder(chunk, divisor)
    if (i < shortBlocks) chunk.push(0)
    all.push(chunk.concat(ecc))
  }
  const result: number[] = []
  for (let i = 0; i < all[0]!.length; i++) {
    all.forEach((block, j) => {
      if (i !== shortLen - eccLen || j >= shortBlocks) result.push(block[i]!)
    })
  }
  return result
}

// ---------------------------------------------------------------------------- the matrix
class Builder {
  readonly version: number
  readonly size: number
  readonly modules: boolean[][]
  readonly reserved: boolean[][]

  constructor(version: number) {
    this.version = version
    this.size = version * 4 + 17
    this.modules = Array.from({ length: this.size }, () => new Array<boolean>(this.size).fill(false))
    this.reserved = Array.from({ length: this.size }, () => new Array<boolean>(this.size).fill(false))
  }

  private set(x: number, y: number, dark: boolean): void {
    this.modules[y]![x] = dark
    this.reserved[y]![x] = true
  }

  drawFunctionPatterns(): void {
    const size = this.size
    for (let i = 0; i < size; i++) {
      this.set(6, i, i % 2 === 0)
      this.set(i, 6, i % 2 === 0)
    }
    for (const [cx, cy] of [
      [3, 3],
      [size - 4, 3],
      [3, size - 4],
    ] as const) {
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const x = cx + dx
          const y = cy + dy
          const dist = Math.max(Math.abs(dx), Math.abs(dy))
          if (x >= 0 && x < size && y >= 0 && y < size) this.set(x, y, dist !== 2 && dist !== 4)
        }
      }
    }
    const positions = alignmentPositions(this.version)
    const n = positions.length
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) {
        if ((i === 0 && j === 0) || (i === 0 && j === n - 1) || (i === n - 1 && j === 0)) continue
        for (let dy = -2; dy <= 2; dy++) {
          for (let dx = -2; dx <= 2; dx++) this.set(positions[i]! + dx, positions[j]! + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1)
        }
      }
    }
    this.drawFormat(0) // reserve the format areas; drawn for real once the mask is chosen
    if (this.version >= 7) {
      const bits = versionBits(this.version)
      for (let i = 0; i < 18; i++) {
        const a = size - 11 + (i % 3)
        const b = Math.floor(i / 3)
        this.set(a, b, bit(bits, i))
        this.set(b, a, bit(bits, i))
      }
    }
  }

  drawFormat(mask: number): void {
    const bits = formatBits(mask)
    const size = this.size
    for (let i = 0; i <= 5; i++) this.set(8, i, bit(bits, i))
    this.set(8, 7, bit(bits, 6))
    this.set(8, 8, bit(bits, 7))
    this.set(7, 8, bit(bits, 8))
    for (let i = 9; i < 15; i++) this.set(14 - i, 8, bit(bits, i))
    for (let i = 0; i < 8; i++) this.set(size - 1 - i, 8, bit(bits, i))
    for (let i = 8; i < 15; i++) this.set(8, size - 15 + i, bit(bits, i))
    this.set(8, size - 8, true) // the dark module
  }

  drawCodewords(codewords: number[]): void {
    const size = this.size
    let i = 0
    for (let right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5
      for (let vert = 0; vert < size; vert++) {
        for (let j = 0; j < 2; j++) {
          const x = right - j
          const upward = ((right + 1) & 2) === 0
          const y = upward ? size - 1 - vert : vert
          if (!this.reserved[y]![x] && i < codewords.length * 8) {
            this.modules[y]![x] = bit(codewords[i >>> 3]!, 7 - (i & 7))
            i++
          }
        }
      }
    }
  }

  applyMask(mask: number): void {
    for (let y = 0; y < this.size; y++) {
      for (let x = 0; x < this.size; x++) {
        if (!this.reserved[y]![x] && maskBit(mask, x, y)) this.modules[y]![x] = !this.modules[y]![x]
      }
    }
  }
}

function maskBit(mask: number, x: number, y: number): boolean {
  switch (mask) {
    case 0:
      return (x + y) % 2 === 0
    case 1:
      return y % 2 === 0
    case 2:
      return x % 3 === 0
    case 3:
      return (x + y) % 3 === 0
    case 4:
      return (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0
    case 5:
      return ((x * y) % 2) + ((x * y) % 3) === 0
    case 6:
      return (((x * y) % 2) + ((x * y) % 3)) % 2 === 0
    default:
      return (((x + y) % 2) + ((x * y) % 3)) % 2 === 0
  }
}

const FINDER_LIKE = [
  [true, false, true, true, true, false, true, false, false, false, false],
  [false, false, false, false, true, false, true, true, true, false, true],
]

/** The standard's penalty score (runs, 2x2 blocks, finder-like patterns, dark balance): lower is easier to read. */
export function penalty(modules: boolean[][]): number {
  const size = modules.length
  let score = 0
  const line = (get: (i: number) => boolean): void => {
    let run = 1
    for (let i = 1; i <= size; i++) {
      if (i < size && get(i) === get(i - 1)) {
        run++
        continue
      }
      if (run >= 5) score += 3 + (run - 5)
      run = 1
    }
    for (let i = 0; i + 11 <= size; i++) {
      for (const pattern of FINDER_LIKE) if (pattern.every((dark, k) => get(i + k) === dark)) score += 40
    }
  }
  for (let y = 0; y < size; y++) line((x) => modules[y]![x]!)
  for (let x = 0; x < size; x++) line((y) => modules[y]![x]!)
  let dark = 0
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const here = modules[y]![x]!
      if (here) dark++
      if (x + 1 < size && y + 1 < size && here === modules[y]![x + 1] && here === modules[y + 1]![x] && here === modules[y + 1]![x + 1]) score += 3
    }
  }
  const total = size * size
  score += Math.floor(Math.abs(dark * 20 - total * 10) / total) * 10
  return score
}

/** Encode `text` (UTF-8) as a QR code at error correction level M in the smallest version that fits. */
export function encodeQr(text: string): QrMatrix {
  const bytes = new TextEncoder().encode(text)
  let version = 1
  while (version <= 40 && 4 + (version <= 9 ? 8 : 16) + bytes.length * 8 > dataCodewords(version) * 8) version++
  if (version > 40) throw new Error('too long for a QR code')
  const codewords = interleave(encodeData(bytes, version), version)
  let best: QrMatrix | null = null
  let bestScore = Infinity
  for (let mask = 0; mask < 8; mask++) {
    const builder = new Builder(version)
    builder.drawFunctionPatterns()
    builder.drawCodewords(codewords)
    builder.applyMask(mask)
    builder.drawFormat(mask)
    const score = penalty(builder.modules)
    if (score < bestScore) {
      bestScore = score
      best = { version, size: builder.size, mask, modules: builder.modules }
    }
  }
  return best!
}

/** An SVG path (`M x y h1v1h-1z` per dark module) offset by a quiet zone of `margin` modules. */
export function qrPath(matrix: QrMatrix, margin = 4): string {
  const parts: string[] = []
  matrix.modules.forEach((row, y) => {
    row.forEach((dark, x) => {
      if (dark) parts.push(`M${x + margin} ${y + margin}h1v1h-1z`)
    })
  })
  return parts.join('')
}
