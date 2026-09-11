/**
 * Pure geometry behind the plan minimap.
 *
 * Separated from the component so it can be tested without a DOM, a rendered SVG or a browser.
 * All three functions are the kind that look right in a screenshot and are wrong by a constant —
 * a cone rotated 90 degrees, a click that lands one letterbox band too low. None of those raise
 * an error; they just send the operator somewhere they did not point at.
 */

/** The subset of `DOMRect` these functions need, so tests need not build a real one. */
export interface BoxRect {
  left: number
  top: number
  width: number
  height: number
}

export interface PlanSize {
  width: number
  height: number
}

export interface MinimapPoint {
  frameId: string
  x: number
  y: number
}

/**
 * Convert a click in client coordinates to plan pixels.
 *
 * The SVG uses `preserveAspectRatio="xMidYMid meet"`, so the drawing is scaled to fit and
 * *centred*, leaving bands on two sides whenever the box and the plan disagree on aspect ratio.
 * Dividing by the box size alone ignores those bands, which is the standard way this goes wrong.
 */
export function planCoordsFromClick(
  clientX: number,
  clientY: number,
  rect: BoxRect,
  plan: PlanSize,
): { x: number; y: number } {
  const scale = Math.min(rect.width / plan.width, rect.height / plan.height)
  const offsetX = (rect.width - plan.width * scale) / 2
  const offsetY = (rect.height - plan.height * scale) / 2
  return {
    x: (clientX - rect.left - offsetX) / scale,
    y: (clientY - rect.top - offsetY) / scale,
  }
}

/**
 * The walked point closest to (x, y), or null if there is nowhere to go.
 *
 * Squared distance, because the square root would be computed only to be thrown away by the
 * comparison.
 */
export function nearestPoint(
  points: readonly MinimapPoint[],
  x: number,
  y: number,
): MinimapPoint | null {
  let best: MinimapPoint | null = null
  let bestDistance = Infinity
  for (const point of points) {
    const distance = (point.x - x) ** 2 + (point.y - y) ** 2
    if (distance < bestDistance) {
      bestDistance = distance
      best = point
    }
  }
  return best
}

/**
 * A wedge of `fov` degrees opening *upward* from (x, y), for the caller to rotate into place.
 *
 * Upward is deliberate: the caller applies the yaw as an SVG rotation, so the untransformed shape
 * must point at 0 degrees. Any built-in offset here becomes a constant error in the heading the
 * operator sees on the plan.
 */
export function coneWedge(x: number, y: number, length: number, fov: number): string {
  const clamped = Math.min(Math.max(fov, 10), 170)
  const half = (clamped / 2) * (Math.PI / 180)
  const leftX = x - Math.sin(half) * length
  const rightX = x + Math.sin(half) * length
  // Plan y grows downward, so "up" is a smaller y.
  const tipY = y - Math.cos(half) * length
  return `M${x} ${y} L${leftX} ${tipY} A${length} ${length} 0 0 1 ${rightX} ${tipY} Z`
}
