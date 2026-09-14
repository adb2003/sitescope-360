/**
 * How far apart the anchors are, and whether that is far enough to trust the fit.
 *
 * Its own module so it can be tested as arithmetic. The rule is easy to get subtly wrong in three
 * separate ways — measuring one axis instead of the diagonal, comparing the closest pair instead
 * of the widest, and warning about a selection that is merely unfinished — and none of them would
 * be visible in a screenshot.
 */

export interface AnchorPoint {
  frameId: string
  x: number
  y: number
}

/**
 * Under this many plan pixels apart, the fit is too sensitive to be worth trusting.
 *
 * Scale and rotation come entirely from the vector between two anchors, so the shorter that
 * vector, the more a few pixels of placement error inflate the scale — and the scale multiplies
 * across every frame in the walk. 80px is roughly a capture point's own diameter on the demo
 * plans: close enough that the operator could plausibly have meant the same spot twice.
 */
export const MIN_USEFUL_ANCHOR_SPAN_PX = 80

/**
 * True when every anchor sits within {@link MIN_USEFUL_ANCHOR_SPAN_PX} of every other.
 *
 * The *widest* pair decides it, not the closest: two anchors together plus a third far away is a
 * well-determined fit, and warning about it would teach the operator to dismiss the warning.
 *
 * Fewer than two anchors is never "close" — that selection is unfinished, and the panel is
 * already asking for another one. Reporting it as a problem would put a warning on screen before
 * the operator had done anything wrong.
 */
export function anchorsAreClose(
  anchors: readonly AnchorPoint[],
  threshold = MIN_USEFUL_ANCHOR_SPAN_PX,
): boolean {
  if (anchors.length < 2) return false

  let widest = 0
  for (let i = 0; i < anchors.length; i += 1) {
    for (let j = i + 1; j < anchors.length; j += 1) {
      const a = anchors[i]!
      const b = anchors[j]!
      // Euclidean, not per-axis: two points 60px across and 60px down are 85px apart, which is a
      // perfectly usable baseline even though neither axis reaches the threshold on its own.
      widest = Math.max(widest, Math.hypot(a.x - b.x, a.y - b.y))
    }
  }
  return widest < threshold
}
