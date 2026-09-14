/**
 * The warning that fires before a useless fit is submitted.
 *
 * Scale and rotation come entirely from the distance and direction between two anchors. Two
 * anchors a few pixels apart still produce a mathematically valid similarity transform — and a
 * wildly wrong one, because a small error in either point becomes a large error in the scale it
 * implies, which then multiplies across every frame in the walk. The server cannot refuse it
 * (the fit converges and its residual is small), so the check has to happen here.
 */

import { describe, expect, it } from 'vitest'

import { MIN_USEFUL_ANCHOR_SPAN_PX, anchorsAreClose } from './anchorSpan'

const at = (frameId: string, x: number, y: number) => ({ frameId, x, y })

describe('anchorsAreClose', () => {
  it('warns about two anchors dropped on nearly the same spot', () => {
    expect(anchorsAreClose([at('a', 100, 100), at('b', 110, 105)])).toBe(true)
  })

  it('is quiet when the anchors span a useful distance', () => {
    expect(anchorsAreClose([at('a', 100, 100), at('b', 600, 400)])).toBe(false)
  })

  it('measures the widest pair, not the closest', () => {
    // Two anchors sit together and a third is far away. The fit is well determined by the far
    // pair, so warning here would train the operator to ignore the warning.
    const anchors = [at('a', 100, 100), at('b', 108, 100), at('c', 700, 300)]

    expect(anchorsAreClose(anchors)).toBe(false)
  })

  it('measures diagonally, not along one axis', () => {
    // 60px across and 60px down is 85px apart — above the threshold, though neither axis is.
    expect(anchorsAreClose([at('a', 0, 0), at('b', 60, 60)])).toBe(false)
    expect(anchorsAreClose([at('a', 0, 0), at('b', 60, 0)])).toBe(true)
  })

  it('treats the threshold itself as too close', () => {
    // The boundary falls on one side deliberately: at exactly the limit the fit is still
    // sensitive enough to be worth mentioning.
    expect(anchorsAreClose([at('a', 0, 0), at('b', MIN_USEFUL_ANCHOR_SPAN_PX, 0)])).toBe(false)
    expect(anchorsAreClose([at('a', 0, 0), at('b', MIN_USEFUL_ANCHOR_SPAN_PX - 1, 0)])).toBe(true)
  })

  it('does not warn before there is anything to compare', () => {
    // Zero or one anchor is an unfinished selection, not a bad one; the panel is already telling
    // the operator to add another.
    expect(anchorsAreClose([])).toBe(false)
    expect(anchorsAreClose([at('a', 10, 10)])).toBe(false)
  })
})
