// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { arcPath, arcs, labelEvery, niceStep, niceTicks, stack } from './scale'

describe('chart scales', () => {
  it('picks nice steps', () => {
    expect(niceStep(0.7)).toBe(1)
    expect(niceStep(1.7)).toBe(2)
    expect(niceStep(23)).toBe(25)
    expect(niceStep(420)).toBe(500)
  })

  it('makes ticks covering the max', () => {
    expect(niceTicks(0)).toEqual([0, 1])
    expect(niceTicks(95)).toEqual([0, 25, 50, 75, 100])
    const ticks = niceTicks(1234)
    expect(ticks[0]).toBe(0)
    expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(1234)
  })

  it('thins labels to fit', () => {
    expect(labelEvery(90, 900, 60)).toBe(6)
    expect(labelEvery(5, 900, 60)).toBe(1)
  })

  it('stacks series', () => {
    expect(stack([[1, 2], [3, 4]])).toEqual([
      [[0, 1], [0, 2]],
      [[1, 4], [2, 6]],
    ])
  })

  it('splits a circle into proportional arcs', () => {
    const a = arcs([1, 1], 0)
    expect(a[0]!.end - a[0]!.start).toBeCloseTo(Math.PI)
    expect(a[1]!.end).toBeCloseTo(Math.PI * 2)
    expect(arcs([0, 0])).toEqual([{ start: 0, end: 0 }, { start: 0, end: 0 }])
    expect(arcPath(50, 50, 40, 30, 0, 1)).toMatch(/^M50\.00,10\.00A40,40/)
  })
})
