// SPDX-License-Identifier: GPL-3.0-or-later
// Seeded randomness so fixtures (digests, sizes, history) are identical on every load.

/** mulberry32: small, fast, good enough for fixtures. */
export function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** Stable 32-bit hash of a string (FNV-1a). */
export function hash(text: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return h >>> 0
}

/** A deterministic, realistic-looking sha256 hex string for `key`. */
export function fakeSha256(key: string): string {
  const next = rng(hash(key))
  let out = ''
  for (let i = 0; i < 64; i++) out += Math.floor(next() * 16).toString(16)
  return out
}

export function pick<T>(items: readonly T[], next: () => number = Math.random): T {
  return items[Math.floor(next() * items.length)] as T
}

/** Uniform in [min, max). */
export function between(min: number, max: number, next: () => number = Math.random): number {
  return min + (max - min) * next()
}

/** Log-uniform in [min, max): good for file sizes, which span orders of magnitude. */
export function logBetween(min: number, max: number, next: () => number = Math.random): number {
  return Math.exp(between(Math.log(min), Math.log(max), next))
}

export const KiB = 1024
export const MiB = 1024 * KiB
export const GiB = 1024 * MiB
