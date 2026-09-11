/**
 * Tests for the minimap's two pieces of geometry.
 *
 * Both are the kind of thing that looks right in a screenshot and is wrong by a constant: a
 * cone pointing 90 degrees off, or a click that lands on the wrong point because the SVG's
 * letterboxing was not accounted for. Neither shows up as an error — they just quietly send the
 * operator somewhere they did not ask to go.
 */

import { describe, expect, it } from 'vitest'

import { coneWedge, nearestPoint, planCoordsFromClick } from './minimapGeometry'

describe('planCoordsFromClick', () => {
  const plan = { width: 1000, height: 500 }

  it('maps a click to plan pixels when the box matches the plan aspect', () => {
    const rect = { left: 0, top: 0, width: 200, height: 100 }
    expect(planCoordsFromClick(100, 50, rect, plan)).toEqual({ x: 500, y: 250 })
  })

  it('accounts for letterboxing when the box is taller than the plan', () => {
    // preserveAspectRatio="xMidYMid meet" centres the drawing and leaves bands above and below.
    // Ignoring the offset is the classic version of this bug: every click lands too low.
    const rect = { left: 0, top: 0, width: 200, height: 200 }
    const centre = planCoordsFromClick(100, 100, rect, plan)
    expect(centre.x).toBeCloseTo(500)
    expect(centre.y).toBeCloseTo(250)

    // The very top of the *box* is above the drawing, so it maps outside the plan.
    expect(planCoordsFromClick(100, 0, rect, plan).y).toBeLessThan(0)
  })

  it('accounts for a rect that is not at the page origin', () => {
    const rect = { left: 40, top: 25, width: 200, height: 100 }
    expect(planCoordsFromClick(140, 75, rect, plan)).toEqual({ x: 500, y: 250 })
  })
})

describe('nearestPoint', () => {
  const points = [
    { frameId: 'a', x: 0, y: 0 },
    { frameId: 'b', x: 100, y: 0 },
    { frameId: 'c', x: 100, y: 100 },
  ]

  it('picks the closest point to an arbitrary click', () => {
    expect(nearestPoint(points, 90, 10)?.frameId).toBe('b')
    expect(nearestPoint(points, 10, 90)?.frameId).toBe('a')
    expect(nearestPoint(points, 95, 95)?.frameId).toBe('c')
  })

  it('returns null when there is nowhere to go', () => {
    expect(nearestPoint([], 10, 10)).toBeNull()
  })

  it('snaps from far outside the points, because intent is still clear', () => {
    // Clicking past the edge of the walked area means "that way"; refusing would be pedantic.
    expect(nearestPoint(points, 5000, 5000)?.frameId).toBe('c')
  })
})

describe('coneWedge', () => {
  it('opens upward so the caller’s rotation is exactly the heading', () => {
    // Untransformed, the wedge must point at 0 degrees: any built-in offset becomes a constant
    // error in the direction shown on the plan.
    const path = coneWedge(50, 50, 20, 60)
    const numbers = path.match(/-?\d+(\.\d+)?/g)!.map(Number)
    // M x y L leftX tipY ...
    expect(numbers[0]).toBe(50)
    expect(numbers[1]).toBe(50)
    expect(numbers[2]).toBeLessThan(50) // left edge is to the left
    expect(numbers[3]).toBeLessThan(50) // and above, since plan y grows downward
  })

  it('clamps an absurd field of view rather than drawing a broken wedge', () => {
    expect(() => coneWedge(0, 0, 10, 0)).not.toThrow()
    expect(() => coneWedge(0, 0, 10, 359)).not.toThrow()
    expect(coneWedge(0, 0, 10, 0)).toContain('M0 0')
  })
})
