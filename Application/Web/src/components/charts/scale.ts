// SPDX-License-Identifier: GPL-3.0-or-later
// Axis arithmetic shared by the chart components (after DeviceVault components/charts).

/** Round `value` up to 1, 2, 2.5 or 5 times a power of ten. */
export function niceStep(value: number): number {
  if (!(value > 0)) return 1
  const power = 10 ** Math.floor(Math.log10(value))
  const fraction = value / power
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10
  return nice * power
}

/** Evenly spaced ticks from 0 covering `max`, about `count` of them; [0, 1] with no data. */
export function niceTicks(max: number, count = 4): number[] {
  if (!(max > 0)) return [0, 1]
  const step = niceStep(max / count)
  const ticks: number[] = []
  for (let tick = 0; tick < max + step / 2; tick += step) ticks.push(Number(tick.toPrecision(12)))
  if (ticks[ticks.length - 1]! < max) ticks.push(Number((ticks.length * step).toPrecision(12)))
  return ticks
}

/** Show every `n`th of `count` labels so that each gets about `minWidth` pixels of `width`. */
export function labelEvery(count: number, width: number, minWidth: number): number {
  if (count <= 0 || width <= 0) return 1
  return Math.max(1, Math.ceil(count / Math.max(1, Math.floor(width / minWidth))))
}

/** Stack series bottom-up: for each series and index, the [lower, upper] bounds. */
export function stack(series: number[][]): [number, number][][] {
  const length = Math.max(0, ...series.map((s) => s.length))
  const base = new Array<number>(length).fill(0)
  return series.map((values) =>
    Array.from({ length }, (_, i) => {
      const lower = base[i]!
      const upper = lower + Math.max(0, values[i] ?? 0)
      base[i] = upper
      return [lower, upper] as [number, number]
    }),
  )
}

/** Donut arcs for values: start/end angles in radians from 12 o'clock, clockwise, with a small gap between. */
export function arcs(values: number[], gap = 0.02): { start: number; end: number }[] {
  const total = values.reduce((a, b) => a + Math.max(0, b), 0)
  if (total <= 0) return values.map(() => ({ start: 0, end: 0 }))
  const shown = values.filter((v) => v > 0).length
  const usable = Math.PI * 2 - (shown > 1 ? gap * shown : 0)
  let angle = 0
  return values.map((v) => {
    const sweep = (Math.max(0, v) / total) * usable
    const out = { start: angle, end: angle + sweep }
    angle += sweep + (v > 0 && shown > 1 ? gap : 0)
    return out
  })
}

/** SVG path for a donut segment between two radii. */
export function arcPath(cx: number, cy: number, outer: number, inner: number, start: number, end: number): string {
  if (end - start <= 0) return ''
  const large = end - start > Math.PI ? 1 : 0
  const p = (r: number, a: number) => `${(cx + r * Math.sin(a)).toFixed(2)},${(cy - r * Math.cos(a)).toFixed(2)}`
  if (end - start >= Math.PI * 2 - 1e-6) {
    // Full circle: two halves.
    const mid = start + Math.PI
    return `M${p(outer, start)}A${outer},${outer} 0 1 1 ${p(outer, mid)}A${outer},${outer} 0 1 1 ${p(outer, start)}M${p(inner, start)}A${inner},${inner} 0 1 0 ${p(inner, mid)}A${inner},${inner} 0 1 0 ${p(inner, start)}Z`
  }
  return `M${p(outer, start)}A${outer},${outer} 0 ${large} 1 ${p(outer, end)}L${p(inner, end)}A${inner},${inner} 0 ${large} 0 ${p(inner, start)}Z`
}
