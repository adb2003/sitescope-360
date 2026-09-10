/**
 * Floor-plan viewer and editor (§12, §13).
 *
 * Renders the plan raster with zoom/pan, capture points, and the capture path polyline.
 * Administrators can add, drag and delete points; everyone can click a point to open its
 * panorama.
 *
 * Coordinates: capture locations are stored in the raster's **pixel** space, which is the only
 * frame of reference that survives zooming, panning and a re-render at a different display size.
 * Every interaction converts screen → plan pixels immediately and works in plan space from
 * there, so a point dropped at 400% zoom lands exactly where the cursor was.
 */

import {
  Crosshair,
  Maximize2,
  Minus,
  Plus,
  Route,
  Trash2,
  ZoomIn,
} from 'lucide-react'
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
} from 'react'

import { Badge, Button, Callout } from '@/components/ui'
import { ProvenanceTag } from '@/components/status'
import { cn, clamp, formatDateTime } from '@/lib/utils'
import type { CaptureLocation, CapturePath, FloorPlan } from '@/types/api'

const MIN_SCALE = 0.05
const MAX_SCALE = 8
/** Below this drag distance a pointer-up counts as a click, not a move. */
const CLICK_THRESHOLD_PX = 4

export interface FloorPlanCanvasProps {
  plan: FloorPlan
  locations: CaptureLocation[]
  paths?: CapturePath[]
  selectedLocationId?: string | null
  /** Enables add / drag / delete. Requires the `location:edit` capability. */
  editable?: boolean
  onSelectLocation?: (location: CaptureLocation) => void
  onAddLocation?: (x: number, y: number) => void
  onMoveLocation?: (id: string, x: number, y: number) => void
  onDeleteLocation?: (id: string) => void
  className?: string
  /** Hide the path polylines even when paths are supplied. */
  hidePaths?: boolean
}

interface Viewport {
  scale: number
  offsetX: number
  offsetY: number
}

export function FloorPlanCanvas({
  plan,
  locations,
  paths = [],
  selectedLocationId,
  editable = false,
  onSelectLocation,
  onAddLocation,
  onMoveLocation,
  onDeleteLocation,
  className,
  hidePaths,
}: FloorPlanCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [viewport, setViewport] = useState<Viewport>({ scale: 1, offsetX: 0, offsetY: 0 })
  const [addMode, setAddMode] = useState(false)
  const [showPaths, setShowPaths] = useState(true)
  const [dragging, setDragging] = useState<{ id: string; x: number; y: number } | null>(null)

  const panState = useRef<{
    pointerId: number
    startX: number
    startY: number
    originX: number
    originY: number
    moved: number
  } | null>(null)
  const dragState = useRef<{ pointerId: number; id: string; moved: number } | null>(null)

  const isRasterUsable = plan.width_px > 0 && plan.height_px > 0

  // ---- fit to container -------------------------------------------------
  const fit = useCallback(() => {
    const container = containerRef.current
    if (!container || !isRasterUsable) return
    const padding = 24
    const scale = Math.min(
      (container.clientWidth - padding) / plan.width_px,
      (container.clientHeight - padding) / plan.height_px,
    )
    const bounded = clamp(scale, MIN_SCALE, MAX_SCALE)
    setViewport({
      scale: bounded,
      offsetX: (container.clientWidth - plan.width_px * bounded) / 2,
      offsetY: (container.clientHeight - plan.height_px * bounded) / 2,
    })
  }, [plan.width_px, plan.height_px, isRasterUsable])

  useEffect(() => {
    fit()
    const container = containerRef.current
    if (!container) return
    const observer = new ResizeObserver(fit)
    observer.observe(container)
    return () => observer.disconnect()
  }, [fit])

  // ---- coordinate conversion --------------------------------------------
  const toPlan = useCallback(
    (clientX: number, clientY: number) => {
      const rect = containerRef.current?.getBoundingClientRect()
      if (!rect) return null
      return {
        x: (clientX - rect.left - viewport.offsetX) / viewport.scale,
        y: (clientY - rect.top - viewport.offsetY) / viewport.scale,
      }
    },
    [viewport],
  )

  const zoomAt = useCallback(
    (factor: number, clientX?: number, clientY?: number) => {
      const rect = containerRef.current?.getBoundingClientRect()
      if (!rect) return
      // Anchor the zoom at the cursor, so the point under the pointer stays put — the behaviour
      // every map interface has, and its absence makes precise placement painful.
      const anchorX = (clientX ?? rect.left + rect.width / 2) - rect.left
      const anchorY = (clientY ?? rect.top + rect.height / 2) - rect.top

      setViewport((current) => {
        const next = clamp(current.scale * factor, MIN_SCALE, MAX_SCALE)
        const ratio = next / current.scale
        return {
          scale: next,
          offsetX: anchorX - (anchorX - current.offsetX) * ratio,
          offsetY: anchorY - (anchorY - current.offsetY) * ratio,
        }
      })
    },
    [],
  )

  const onWheel = useCallback(
    (event: ReactWheelEvent<HTMLDivElement>) => {
      event.preventDefault()
      zoomAt(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX, event.clientY)
    },
    [zoomAt],
  )

  // ---- pan --------------------------------------------------------------
  const onBackgroundPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0 && event.pointerType === 'mouse') return
      panState.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        originX: viewport.offsetX,
        originY: viewport.offsetY,
        moved: 0,
      }
      event.currentTarget.setPointerCapture?.(event.pointerId)
    },
    [viewport.offsetX, viewport.offsetY],
  )

  const onPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const drag = dragState.current
      if (drag && drag.pointerId === event.pointerId) {
        const point = toPlan(event.clientX, event.clientY)
        if (point) {
          drag.moved += 1
          setDragging({
            id: drag.id,
            x: clamp(point.x, 0, plan.width_px),
            y: clamp(point.y, 0, plan.height_px),
          })
        }
        return
      }

      const pan = panState.current
      if (!pan || pan.pointerId !== event.pointerId) return
      const dx = event.clientX - pan.startX
      const dy = event.clientY - pan.startY
      pan.moved = Math.abs(dx) + Math.abs(dy)
      setViewport((current) => ({
        ...current,
        offsetX: pan.originX + dx,
        offsetY: pan.originY + dy,
      }))
    },
    [toPlan, plan.width_px, plan.height_px],
  )

  const onPointerUp = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const drag = dragState.current
      if (drag && drag.pointerId === event.pointerId) {
        dragState.current = null
        const final = dragging
        setDragging(null)
        // Only commit a genuine move: a click on a pin should select it, not rewrite its
        // coordinates with the same values and log a spurious edit.
        if (final && drag.moved > 1 && onMoveLocation) {
          onMoveLocation(drag.id, Math.round(final.x), Math.round(final.y))
        } else if (drag.moved <= 1) {
          const location = locations.find((item) => item.id === drag.id)
          if (location) onSelectLocation?.(location)
        }
        return
      }

      const pan = panState.current
      if (!pan || pan.pointerId !== event.pointerId) return
      panState.current = null

      if (pan.moved < CLICK_THRESHOLD_PX && addMode && onAddLocation) {
        const point = toPlan(event.clientX, event.clientY)
        if (
          point &&
          point.x >= 0 &&
          point.y >= 0 &&
          point.x <= plan.width_px &&
          point.y <= plan.height_px
        ) {
          onAddLocation(Math.round(point.x), Math.round(point.y))
          setAddMode(false)
        }
      }
    },
    [
      dragging,
      onMoveLocation,
      locations,
      onSelectLocation,
      addMode,
      onAddLocation,
      toPlan,
      plan.width_px,
      plan.height_px,
    ],
  )

  const startPinDrag = useCallback(
    (event: ReactPointerEvent<HTMLButtonElement>, location: CaptureLocation) => {
      if (!editable) {
        onSelectLocation?.(location)
        return
      }
      event.stopPropagation()
      dragState.current = { pointerId: event.pointerId, id: location.id, moved: 0 }
      setDragging({ id: location.id, x: location.x, y: location.y })
      event.currentTarget.setPointerCapture?.(event.pointerId)
    },
    [editable, onSelectLocation],
  )

  // ---- path geometry ----------------------------------------------------
  const polylines = useMemo(() => {
    if (hidePaths || !showPaths) return []
    return paths
      .map((path) => ({
        id: path.id,
        provenance: path.provenance,
        points: path.steps
          .filter((step) => step.x !== null && step.y !== null)
          .map((step) => `${step.x},${step.y}`)
          .join(' '),
        count: path.steps.length,
      }))
      .filter((path) => path.count > 1)
  }, [paths, showPaths, hidePaths])

  if (!isRasterUsable) {
    return (
      <div className={cn('panel p-4', className)}>
        <Callout tone="warn" title="This floor plan cannot be used for placement">
          {String(plan.calibration?.note ?? '') ||
            'The plan has no pixel dimensions. Upload a PNG or JPG export of the drawing to place capture points on it.'}
        </Callout>
      </div>
    )
  }

  return (
    <div className={cn('relative overflow-hidden panel', className)}>
      {/* Toolbar */}
      <div className="absolute left-2 top-2 z-10 flex flex-wrap items-center gap-1.5">
        {editable && onAddLocation ? (
          <Button
            size="sm"
            variant={addMode ? 'primary' : 'secondary'}
            icon={Crosshair}
            onClick={() => setAddMode((value) => !value)}
          >
            {addMode ? 'Click the plan…' : 'Add point'}
          </Button>
        ) : null}
        {paths.length && !hidePaths ? (
          <Button
            size="sm"
            variant={showPaths ? 'primary' : 'secondary'}
            icon={Route}
            onClick={() => setShowPaths((value) => !value)}
            title="Show or hide capture paths"
          >
            Path
          </Button>
        ) : null}
      </div>

      <div className="absolute right-2 top-2 z-10 flex items-center gap-1 rounded bg-surface/90 p-1 shadow-panel backdrop-blur">
        <Button variant="ghost" size="icon-sm" onClick={() => zoomAt(1.25)} aria-label="Zoom in">
          <Plus aria-hidden />
        </Button>
        <span className="tnum w-10 text-center text-2xs text-ink-muted">
          {Math.round(viewport.scale * 100)}%
        </span>
        <Button variant="ghost" size="icon-sm" onClick={() => zoomAt(1 / 1.25)} aria-label="Zoom out">
          <Minus aria-hidden />
        </Button>
        <Button variant="ghost" size="icon-sm" onClick={fit} aria-label="Fit to view" title="Fit">
          <Maximize2 aria-hidden />
        </Button>
      </div>

      {plan.px_per_meter ? (
        <ScaleBar pxPerMeter={plan.px_per_meter} scale={viewport.scale} />
      ) : null}

      <div
        ref={containerRef}
        className={cn(
          'relative h-full min-h-[320px] w-full touch-none select-none overflow-hidden bg-surface-raised',
          addMode ? 'cursor-crosshair' : 'cursor-grab',
        )}
        onWheel={onWheel}
        onPointerDown={onBackgroundPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <div
          className="absolute origin-top-left"
          style={{
            width: plan.width_px,
            height: plan.height_px,
            transform: `translate(${viewport.offsetX}px, ${viewport.offsetY}px) scale(${viewport.scale})`,
          }}
        >
          {plan.asset?.url ? (
            <img
              src={plan.asset.url}
              alt={`Floor plan: ${plan.name}`}
              width={plan.width_px}
              height={plan.height_px}
              draggable={false}
              className="pointer-events-none absolute inset-0 h-full w-full"
            />
          ) : null}

          {/* Capture path. Vector-drawn in plan space so it scales exactly with the raster. */}
          {polylines.length ? (
            <svg
              className="pointer-events-none absolute inset-0"
              width={plan.width_px}
              height={plan.height_px}
              viewBox={`0 0 ${plan.width_px} ${plan.height_px}`}
              aria-hidden
            >
              {polylines.map((path) => (
                <polyline
                  key={path.id}
                  data-capture-path={path.id}
                  data-provenance={path.provenance}
                  points={path.points}
                  fill="none"
                  stroke="rgb(var(--brand))"
                  strokeWidth={3 / viewport.scale}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  // An estimated path is dashed. A solid line would imply the platform knows the
                  // route with the same certainty as a manually placed one (§46).
                  strokeDasharray={
                    path.provenance === 'estimated' ? `${8 / viewport.scale} ${6 / viewport.scale}` : undefined
                  }
                  opacity={0.85}
                />
              ))}
            </svg>
          ) : null}
        </div>

        {/* Pins live outside the scaled wrapper so they stay a constant screen size. */}
        {locations.map((location) => {
          const live = dragging?.id === location.id ? dragging : location
          const left = viewport.offsetX + live.x * viewport.scale
          const top = viewport.offsetY + live.y * viewport.scale
          const selected = location.id === selectedLocationId
          const visited = location.frame_count > 0

          return (
            <button
              key={location.id}
              type="button"
              onPointerDown={(event) => startPinDrag(event, location)}
              style={{ left, top }}
              className={cn(
                'group absolute z-[5] -translate-x-1/2 -translate-y-1/2 rounded-full border-2 text-[10px] font-bold shadow-panel transition-transform',
                'flex size-6 items-center justify-center',
                selected
                  ? 'scale-125 border-white bg-brand text-brand-ink ring-2 ring-brand'
                  : visited
                    ? 'border-white/85 bg-brand text-brand-ink hover:scale-110'
                    : 'border-white/70 bg-ink-subtle text-white hover:scale-110',
                editable ? 'cursor-move' : 'cursor-pointer',
              )}
              title={[
                location.code,
                location.label,
                visited
                  ? `${location.frame_count} capture(s), latest ${formatDateTime(location.latest_captured_at)}`
                  : 'Never captured',
              ]
                .filter(Boolean)
                .join(' — ')}
            >
              {location.code.replace(/^P/, '')}
              {location.provenance === 'estimated' ? (
                <span
                  className="absolute -right-0.5 -top-0.5 size-2 rounded-full bg-estimated ring-1 ring-surface"
                  title="Position estimated by computer vision"
                />
              ) : null}
            </button>
          )
        })}
      </div>

      {/* Legend + selection actions */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-line px-3 py-2 text-2xs text-ink-muted">
        <span className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-full bg-brand" /> Captured
        </span>
        <span className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-full bg-ink-subtle" /> Never captured
        </span>
        <span className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-full bg-estimated" /> Estimated position
        </span>
        <span className="ml-auto flex items-center gap-2">
          <ZoomIn className="size-3" aria-hidden />
          Scroll to zoom, drag to pan
        </span>
        {editable && selectedLocationId && onDeleteLocation ? (
          <Button
            size="sm"
            variant="danger"
            icon={Trash2}
            onClick={() => onDeleteLocation(selectedLocationId)}
          >
            Delete point
          </Button>
        ) : null}
      </div>
    </div>
  )
}

// ---- scale bar --------------------------------------------------------------
/**
 * Shown only when the plan has been calibrated. Without a calibration there is no honest way to
 * put a metre bar on a drawing, so none is drawn (§12, §46).
 */
function ScaleBar({ pxPerMeter, scale }: { pxPerMeter: number; scale: number }) {
  // Pick a round number of metres that lands between 60 and 160 screen pixels.
  const candidates = [1, 2, 5, 10, 20, 50, 100]
  const metres =
    candidates.find((value) => value * pxPerMeter * scale >= 60) ??
    candidates[candidates.length - 1] ??
    10
  const widthPx = metres * pxPerMeter * scale

  return (
    <div className="pointer-events-none absolute bottom-12 left-3 z-10 rounded bg-surface/90 px-2 py-1 backdrop-blur">
      <div className="flex items-center gap-1.5">
        <div className="relative h-1.5" style={{ width: Math.min(widthPx, 200) }}>
          <div className="absolute inset-x-0 bottom-0 h-px bg-ink" />
          <div className="absolute bottom-0 left-0 h-1.5 w-px bg-ink" />
          <div className="absolute bottom-0 right-0 h-1.5 w-px bg-ink" />
        </div>
        <span className="tnum text-2xs text-ink-muted">{metres} m</span>
      </div>
    </div>
  )
}

// ---- location list ----------------------------------------------------------
export function LocationList({
  locations,
  selectedLocationId,
  onSelect,
  className,
}: {
  locations: CaptureLocation[]
  selectedLocationId?: string | null
  onSelect: (location: CaptureLocation) => void
  className?: string
}) {
  if (!locations.length) {
    return (
      <p className={cn('px-3 py-6 text-center text-sm text-ink-muted', className)}>
        No capture points yet. Add them on the floor plan.
      </p>
    )
  }

  return (
    <ul className={cn('divide-y divide-line', className)}>
      {locations.map((location) => (
        <li key={location.id}>
          <button
            type="button"
            onClick={() => onSelect(location)}
            className={cn(
              'flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-surface-raised',
              location.id === selectedLocationId && 'bg-brand-soft',
            )}
          >
            <Badge tone={location.frame_count > 0 ? 'brand' : 'neutral'}>{location.code}</Badge>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm text-ink">
                {location.label ?? 'Unlabelled'}
              </span>
              <span className="block text-2xs text-ink-subtle">
                {location.frame_count > 0
                  ? `${location.frame_count} capture(s) · ${formatDateTime(location.latest_captured_at)}`
                  : 'Never captured'}
              </span>
            </span>
            <ProvenanceTag provenance={location.provenance} confidence={location.confidence} />
          </button>
        </li>
      ))}
    </ul>
  )
}
