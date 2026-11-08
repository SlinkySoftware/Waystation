// SPDX-License-Identifier: GPL-3.0-or-later
// Number, size, rate and time formatting (UI_DESIGN §2): binary units for sizes, SI for rates, three significant
// digits, relative times with an absolute tooltip.

const BINARY = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
const SI_RATE = ['B/s', 'kB/s', 'MB/s', 'GB/s', 'TB/s']

/** Three significant digits, no trailing zeros beyond what is needed: 1.23, 12.3, 123. */
export function sig3(value: number): string {
  if (!Number.isFinite(value)) return '–'
  const abs = Math.abs(value)
  if (abs >= 100) return Math.round(value).toString()
  if (abs >= 10) return value.toFixed(1)
  return value.toFixed(2)
}

/** Bytes in binary units: 0 B, 512 B, 1.50 KiB, 23.4 MiB, 1.21 GiB. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '–'
  if (Math.abs(bytes) < 1024) return `${Math.round(bytes)} B`
  let value = bytes
  let unit = 0
  while (Math.abs(value) >= 1024 && unit < BINARY.length - 1) {
    value /= 1024
    unit++
  }
  // 1023.6 KiB would print as "1024 KiB": promote instead.
  if (Math.abs(value) >= 999.5 && unit < BINARY.length - 1) {
    value /= 1024
    unit++
  }
  return `${sig3(value)} ${BINARY[unit]}`
}

/** Bytes per second in SI units: 950 B/s, 12.5 MB/s, 1.10 GB/s. */
export function formatRate(bytesPerSecond: number | null | undefined): string {
  if (bytesPerSecond === null || bytesPerSecond === undefined || !Number.isFinite(bytesPerSecond)) return '–'
  let value = bytesPerSecond
  let unit = 0
  while (Math.abs(value) >= 1000 && unit < SI_RATE.length - 1) {
    value /= 1000
    unit++
  }
  if (Math.abs(value) >= 999.5 && unit < SI_RATE.length - 1) {
    value /= 1000
    unit++
  }
  return unit === 0 ? `${Math.round(value)} B/s` : `${sig3(value)} ${SI_RATE[unit]}`
}

/** Counts: 950, 1.2K, 34.5K, 1.2M (compact above 9,999). */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '–'
  if (Math.abs(value) < 10000) return new Intl.NumberFormat('en').format(Math.round(value))
  return new Intl.NumberFormat('en', { notation: 'compact', maximumSignificantDigits: 3 }).format(value)
}

/** Requests per second: 0.4, 12.3, 1.2K. */
export function formatPerSecond(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '–'
  if (value < 1000) return sig3(value).replace(/\.0+$/, '')
  return formatCount(value)
}

/** A 0..1 ratio as a percentage with one decimal under 100: 87.4 %. */
export function formatPercent(ratio: number | null | undefined, digits = 1): string {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return '–'
  const pct = ratio * 100
  if (pct >= 99.95 && pct < 100) return '>99.9%'
  return `${pct >= 100 ? 100 : pct.toFixed(digits)}%`
}

/** Seconds as a short duration: 0.8 s, 42 s, 3m 05s, 2h 10m, 3d 4h. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return '–'
  if (seconds < 10) return `${seconds.toFixed(1)} s`
  if (seconds < 60) return `${Math.round(seconds)} s`
  const s = Math.round(seconds)
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
  if (s < 86400) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`
}

/** Milliseconds latency: 4 ms, 120 ms, 1.2 s. */
export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '–'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

/** Remaining time for a transfer, or null when unknown. */
export function etaSeconds(done: number, total: number | null | undefined, rate: number): number | null {
  if (!total || total <= done || rate <= 0) return null
  return (total - done) / rate
}

const RELATIVE = new Intl.RelativeTimeFormat('en', { numeric: 'auto', style: 'short' })

/** "just now", "42 sec. ago", "5 min. ago", "3 hr. ago", "yesterday", "4 days ago"; absolute after 30 days. */
export function relativeTime(iso: string | Date | null | undefined, now: number = Date.now()): string {
  if (!iso) return '–'
  const t = typeof iso === 'string' ? Date.parse(iso) : iso.getTime()
  if (Number.isNaN(t)) return '–'
  const diff = Math.round((t - now) / 1000)
  const abs = Math.abs(diff)
  if (abs < 10) return 'just now'
  if (abs < 60) return RELATIVE.format(diff, 'second')
  if (abs < 3600) return RELATIVE.format(Math.round(diff / 60), 'minute')
  if (abs < 86400) return RELATIVE.format(Math.round(diff / 3600), 'hour')
  if (abs < 86400 * 30) return RELATIVE.format(Math.round(diff / 86400), 'day')
  return absoluteDate(new Date(t))
}

/** Absolute timestamp in the user's locale and timezone, e.g. "8 Nov 2026, 14:03:12". */
export function absoluteTime(iso: string | Date | null | undefined): string {
  if (!iso) return ''
  const d = typeof iso === 'string' ? new Date(iso) : iso
  if (Number.isNaN(d.getTime())) return ''
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium' }).format(d)
}

export function absoluteDate(d: Date): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(d)
}

/** Clock label for chart axes: "14:05" or "Mon 14:00" or "8 Nov". */
export function axisTime(iso: string, stepSeconds: number): string {
  const d = new Date(iso)
  if (stepSeconds >= 6 * 3600) return new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' }).format(d)
  if (stepSeconds >= 3600) return new Intl.DateTimeFormat(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' }).format(d)
  if (stepSeconds < 60) return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(d)
  return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(d)
}

/** Shorten in the middle, keeping both ends: "library/nginx@sha256:3f1a…9c2e". */
export function truncateMiddle(text: string, max: number): string {
  if (text.length <= max) return text
  if (max < 5) return text.slice(0, max)
  const keep = max - 1
  const head = Math.ceil(keep * 0.6)
  return `${text.slice(0, head)}…${text.slice(text.length - (keep - head))}`
}

/** "sha256:3f1a2b3c4d5e" (12 hex chars) from a full digest. */
export function shortDigest(digest: string | null | undefined, length = 12): string {
  if (!digest) return '–'
  const [algo, hex] = digest.includes(':') ? digest.split(':', 2) : ['sha256', digest]
  return `${algo}:${(hex ?? '').slice(0, length)}`
}
