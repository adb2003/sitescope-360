/**
 * Floor-plan minimap overlaid on the 360 viewer (§12, §13).
 *
 * The interaction this exists for: **point at a place on the plan, stand there.**
 *
 * Not a replacement for `FloorPlanCanvas`, which already does plan-click-to-jump in the viewer's
 * side panel and stays the right tool for *editing* — pan, zoom, add, move and delete points.
 * This is a HUD, and the jobs pull in opposite directions: an editor wants drag-to-pan and exact
 * hit targets, a HUD wants a glanceable corner overlay you can click without aiming. Three things
 * are genuinely new here rather than moved: the overlay position, the facing cone, and
 * snap-to-nearest.
 *
 * Three things it draws, and the distinction between them is the whole point:
 *
 * - the **path** actually walked in the capture being viewed
 * - the **position** you are standing at, with a cone showing which way you are facing
 * - **other locations** on this floor that this capture did not visit, drawn dim and not
 *   clickable, because offering them would promise imagery that does not exist
 *
 * Clicking anywhere snaps to the nearest walked point rather than requiring a hit on a dot: a
 * 9 px target inside a 240 px overlay is a mouse-accuracy test, and the operator's intent
 * ("somewhere over there") is perfectly clear without one.
 */

import { useMemo, useRef, useState } from 'react'

import { cn } from '@/lib/utils'
import { coneWedge, nearestPoint, planCoordsFromClick } from './minimapGeometry'
import type { CaptureFrame, CaptureLocation, FloorPlan } from '@/types/api'

/** Where the viewer is looking, so the cone can follow it. */
export interface MinimapOrientation {
  yaw: number
  fov: number
}

interface PlanMinimapProps {
  plan: FloorPlan
  /** Every location on the floor; ones this capture did not visit are shown but not clickable. */
  locations: CaptureLocation[]
  /** Frames of the capture being viewed, in walk order. */
  frames: CaptureFrame[]
  activeFrameId: string | null
  orientation?: MinimapOrientation
  onSelectFrame: (frameId: string) => void
  /**
   * Every click, in plan pixels — whether or not it landed near a walked point.
   *
   * Separate from `onSelectFrame` because the two answer different questions. Jumping to the
   * nearest frame of *this* walkthrough has to be instant, so it is resolved here from points
   * already in memory. Asking what else was captured near that spot, on other dates, is a
   * question only the server can answer, and it is asked about the coordinate the operator
   * pointed at rather than about whichever frame happened to be closest.
   */
  onProbe?: (point: { x: number; y: number }) => void
  className?: string
}

/** A point on the plan the viewer can actually jump to. */
interface WalkedPoint {
  frameId: string
  x: number
  y: number
  sequence: number
  code: string
}

export function PlanMinimap({
  plan,
  locations,
  frames,
  activeFrameId,
  orientation,
  onSelectFrame,
  onProbe,
  className,
}: PlanMinimapProps) {
  const [expanded, setExpanded] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const [hovered, setHovered] = useState<string | null>(null)
  const svgRef = useRef<SVGSVGElement>(null)

  const locationById = useMemo(() => {
    const map = new Map<string, CaptureLocation>()
    for (const location of locations) map.set(location.id, location)
    return map
  }, [locations])

  /**
   * Frames that are placed on the plan, in walk order.
   *
   * A frame without a `location_id` has not been anchored, so there is no honest place to draw
   * it. Those are counted and reported rather than dropped silently — "3 of 31 frames are not on
   * the plan" is a fact the operator can act on; a short path with no explanation is not.
   */
  const walked = useMemo(() => {
    const points: WalkedPoint[] = []
    for (const frame of frames) {
      if (!frame.location_id) continue
      const location = locationById.get(frame.location_id)
      if (!location) continue
      points.push({
        frameId: frame.id,
        x: location.x,
        y: location.y,
        sequence: frame.sequence_index,
        code: location.code,
      })
    }
    return points.sort((a, b) => a.sequence - b.sequence)
  }, [frames, locationById])

  const unplacedCount = frames.length - walked.length
  const walkedLocationIds = useMemo(
    () => new Set(walked.map((point) => `${point.x},${point.y}`)),
    [walked],
  )
  const unvisited = useMemo(
    () => locations.filter((location) => !walkedLocationIds.has(`${location.x},${location.y}`)),
    [locations, walkedLocationIds],
  )

  const active = walked.find((point) => point.frameId === activeFrameId) ?? null

  /** Nearest walked point to a click, in plan pixels. See `minimapGeometry` for the maths. */
  const handleClick = (event: React.MouseEvent<SVGSVGElement>) => {
    if (!svgRef.current) return
    const { x, y } = planCoordsFromClick(
      event.clientX,
      event.clientY,
      svgRef.current.getBoundingClientRect(),
      { width: plan.width_px, height: plan.height_px },
    )
    // The probe fires for every click, including ones nowhere near this walkthrough — "nothing
    // was captured here" is a real answer and the operator only gets it if the click is reported.
    onProbe?.({ x, y })

    const best = nearestPoint(walked, x, y)
    if (best && best.frameId !== activeFrameId) onSelectFrame(best.frameId)
  }

  const pathData = useMemo(() => {
    if (walked.length < 2) return null
    return walked.map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x} ${point.y}`).join(' ')
  }, [walked])

  // Sized so a dot stays visible on a large plan and does not swamp a small one.
  const dotRadius = Math.max(plan.width_px, plan.height_px) / 110
  const coneLength = Math.max(plan.width_px, plan.height_px) / 7

  return (
    <div
      className={cn(
        'pointer-events-auto overflow-hidden rounded-lg border border-line bg-surface/95 shadow-lg backdrop-blur',
        expanded ? 'w-[min(78vw,760px)]' : 'w-[clamp(200px,26vw,300px)]',
        className,
      )}
    >
      <div className="flex items-center gap-2 border-b border-line px-2 py-1.5">
        <span className="truncate text-xs font-medium text-ink" title={plan.name}>
          {plan.name}
        </span>
        <span className="ml-auto shrink-0 text-[11px] tabular-nums text-ink-subtle">
          {walked.length} pt
        </span>
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="rounded px-1 text-xs text-ink-muted hover:bg-surface-raised hover:text-ink"
          aria-label={expanded ? 'Shrink plan' : 'Enlarge plan'}
          title={expanded ? 'Shrink' : 'Enlarge'}
        >
          {expanded ? '⤡' : '⤢'}
        </button>
        <button
          type="button"
          onClick={() => setCollapsed((value) => !value)}
          className="rounded px-1 text-xs text-ink-muted hover:bg-surface-raised hover:text-ink"
          aria-label={collapsed ? 'Show plan' : 'Hide plan'}
          aria-expanded={!collapsed}
          title={collapsed ? 'Show' : 'Hide'}
        >
          {collapsed ? '▸' : '▾'}
        </button>
      </div>

      {collapsed ? null : (
        <>
          <svg
            ref={svgRef}
            viewBox={`0 0 ${plan.width_px} ${plan.height_px}`}
            preserveAspectRatio="xMidYMid meet"
            className={cn(
              'block w-full bg-canvas',
              walked.length > 0 ? 'cursor-crosshair' : 'cursor-default',
              expanded ? 'max-h-[70vh]' : 'max-h-[240px]',
            )}
            onClick={handleClick}
            role={walked.length > 0 ? 'button' : undefined}
            aria-label={
              walked.length > 0 ? 'Floor plan — click to move to the nearest capture point' : undefined
            }
          >
            {plan.asset?.url ? (
              <image
                href={plan.asset.url}
                x={0}
                y={0}
                width={plan.width_px}
                height={plan.height_px}
                // The drawing is reference, not the subject: dimming it keeps the path and the
                // position legible on a dense architectural plan.
                opacity={0.75}
              />
            ) : null}

            {/* Locations this capture did not visit: shown for context, deliberately not
                clickable, because there is no imagery to show at them. */}
            {unvisited.map((location) => (
              <circle
                key={location.id}
                cx={location.x}
                cy={location.y}
                r={dotRadius * 0.66}
                className="fill-ink-subtle/35"
              />
            ))}

            {pathData ? (
              <path
                d={pathData}
                fill="none"
                className="stroke-brand/60"
                strokeWidth={dotRadius * 0.7}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            ) : null}

            {walked.map((point) => (
              <circle
                key={point.frameId}
                cx={point.x}
                cy={point.y}
                r={point.frameId === hovered ? dotRadius * 1.25 : dotRadius * 0.85}
                className={cn(
                  'transition-[r]',
                  point.frameId === activeFrameId ? 'fill-transparent' : 'fill-brand',
                )}
                onMouseEnter={() => setHovered(point.frameId)}
                onMouseLeave={() => setHovered((value) => (value === point.frameId ? null : value))}
              >
                <title>{point.code}</title>
              </circle>
            ))}

            {active ? (
              <g>
                {/* Facing cone. Plan y grows downward while yaw is measured clockwise from north,
                    so the rotation is applied about the point and the cone drawn pointing up. */}
                {orientation ? (
                  <g transform={`rotate(${orientation.yaw} ${active.x} ${active.y})`}>
                    <path
                      d={coneWedge(active.x, active.y, coneLength, orientation.fov)}
                      className="fill-estimated/35 stroke-estimated/60"
                      strokeWidth={dotRadius * 0.25}
                    />
                  </g>
                ) : null}
                <circle
                  cx={active.x}
                  cy={active.y}
                  r={dotRadius * 1.35}
                  className="fill-estimated stroke-canvas"
                  strokeWidth={dotRadius * 0.45}
                />
              </g>
            ) : null}
          </svg>

          <div className="flex items-center gap-2 border-t border-line px-2 py-1 text-[11px] text-ink-subtle">
            {walked.length === 0 ? (
              <span>No frames from this capture are placed on the plan yet.</span>
            ) : (
              <>
                <span>Click the plan to move</span>
                {unplacedCount > 0 ? (
                  <span className="ml-auto" title="Frames with no location assigned">
                    {unplacedCount} not placed
                  </span>
                ) : null}
              </>
            )}
          </div>
        </>
      )}
    </div>
  )
}

