/**
 * The full 360 viewer surface (§11): panorama, overlays, filmstrip, and context header.
 *
 * The overlays are plain DOM positioned from `PanoramaHandle.project()` on each orientation
 * change, rather than sprites inside the scene. That is a deliberate trade: DOM pins get real
 * text rendering, hover states, focus order and screen-reader labels for free, and there are
 * never more than a few dozen of them.
 */

import {
  Compass,
  Crosshair,
  Expand,
  Loader2,
  MapPin,
  MessageSquarePlus,
  RotateCcw,
  Ruler,
} from 'lucide-react'
import { useCallback, useMemo, useRef, useState } from 'react'

import { Badge, Button, Callout } from '@/components/ui'
import { ProvenanceTag, QualityBadge } from '@/components/status'
import { cn, formatDateTime, formatPercent, humanize } from '@/lib/utils'
import {
  PanoramaCanvas,
  type Orientation,
  type PanoramaHandle,
} from '@/components/viewer/PanoramaCanvas'
import type { Annotation, CaptureFrameDetail, ViewerManifest } from '@/types/api'

export interface Viewer360Props {
  manifest: ViewerManifest
  annotations?: Annotation[]
  onSelectFrame?: (frameId: string) => void
  onPlaceAnnotation?: (yaw: number, pitch: number) => void
  onSelectAnnotation?: (annotation: Annotation) => void
  /** Mirrors this viewer's orientation out, for the synchronised compare view (§17). */
  onOrientationChange?: (orientation: Orientation) => void
  /** Applied from outside when following another viewer. */
  followOrientation?: Orientation | null
  interactive?: boolean
  className?: string
  compact?: boolean
}

export function Viewer360({
  manifest,
  annotations = [],
  onSelectFrame,
  onPlaceAnnotation,
  onSelectAnnotation,
  onOrientationChange,
  followOrientation,
  interactive = true,
  className,
  compact,
}: Viewer360Props) {
  const canvasRef = useRef<PanoramaHandle>(null)
  const [orientation, setOrientation] = useState<Orientation>({
    yaw: manifest.frame.initial_yaw_deg,
    pitch: 0,
    fov: 74,
  })
  const [loadState, setLoadState] = useState<'loading' | 'preview' | 'ready' | 'error'>('loading')
  const [placing, setPlacing] = useState(false)

  const frame = manifest.frame
  const fullUrl = frame.full?.url
  const previewUrl = frame.preview?.url ?? null

  // Applying a followed orientation imperatively (rather than as a prop the canvas re-reads)
  // avoids a React render per frame while dragging the leader.
  if (followOrientation && canvasRef.current) {
    canvasRef.current.setOrientation(followOrientation)
  }

  const handleOrientation = useCallback(
    (next: Orientation) => {
      setOrientation(next)
      onOrientationChange?.(next)
    },
    [onOrientationChange],
  )

  const handlePick = useCallback(
    (yaw: number, pitch: number) => {
      if (!placing || !onPlaceAnnotation) return
      onPlaceAnnotation(yaw, pitch)
      setPlacing(false)
    },
    [placing, onPlaceAnnotation],
  )

  // Recomputed on every orientation change; cheap for a few dozen markers.
  const projected = useMemo(() => {
    const handle = canvasRef.current
    if (!handle) return { annotations: [], hotspots: [] }

    return {
      annotations: annotations
        .map((annotation) => {
          const point = handle.project(annotation.yaw_deg, annotation.pitch_deg)
          return point ? { annotation, point } : null
        })
        .filter((item): item is { annotation: Annotation; point: { x: number; y: number } } =>
          Boolean(item),
        ),
      hotspots: manifest.neighbours
        .map((neighbour) => {
          if (neighbour.direction_deg === null || neighbour.direction_deg === undefined) return null
          // Hotspots sit slightly below the horizon, where a floor-level waypoint belongs.
          const point = handle.project(neighbour.direction_deg, -18)
          return point ? { neighbour, point } : null
        })
        .filter(
          (
            item,
          ): item is {
            neighbour: ViewerManifest['neighbours'][number]
            point: { x: number; y: number }
          } => Boolean(item),
        ),
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- orientation drives reprojection
  }, [annotations, manifest.neighbours, orientation])

  if (!fullUrl) {
    return (
      <div className={cn('flex items-center justify-center panel p-8', className)}>
        <Callout tone="warn" title="Image unavailable">
          This frame has no accessible image. The stored object may have been removed.
        </Callout>
      </div>
    )
  }

  return (
    <div className={cn('flex min-h-0 flex-col overflow-hidden panel', className)}>
      {!compact ? <ViewerHeader frame={frame} /> : null}

      <div className="relative min-h-0 flex-1 bg-black">
        <PanoramaCanvas
          ref={canvasRef}
          src={fullUrl}
          previewSrc={previewUrl}
          initialYaw={frame.initial_yaw_deg}
          className="absolute inset-0"
          onOrientationChange={handleOrientation}
          onPick={handlePick}
          onLoadStateChange={setLoadState}
          interactive={interactive}
        />

        {/* Annotation pins */}
        {projected.annotations.map(({ annotation, point }) => (
          <button
            key={annotation.id}
            type="button"
            onClick={() => onSelectAnnotation?.(annotation)}
            style={{ left: point.x, top: point.y }}
            className={cn(
              'absolute -translate-x-1/2 -translate-y-1/2 rounded-full p-1 shadow-pop transition-transform hover:scale-110',
              annotation.status === 'resolved'
                ? 'bg-good text-white'
                : annotation.priority === 'critical' || annotation.priority === 'high'
                  ? 'bg-bad text-white'
                  : 'bg-warn text-black',
            )}
            title={`${annotation.title} — ${humanize(annotation.priority)}, ${humanize(annotation.status)}`}
          >
            <MessageSquarePlus className="size-3.5" aria-hidden />
            <span className="sr-only">{annotation.title}</span>
          </button>
        ))}

        {/* Neighbour hotspots — click to walk to the next capture position */}
        {onSelectFrame
          ? projected.hotspots.map(({ neighbour, point }) => (
              <button
                key={neighbour.frame_id}
                type="button"
                onClick={() => onSelectFrame(neighbour.frame_id)}
                style={{ left: point.x, top: point.y }}
                className="absolute -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white/85 bg-brand/85 px-2 py-1 text-2xs font-bold text-white shadow-pop transition-transform hover:scale-110"
                title={
                  neighbour.location_code
                    ? `Walk to ${neighbour.location_code}`
                    : 'Walk to the next capture position'
                }
              >
                <MapPin className="mr-0.5 inline size-3" aria-hidden />
                {neighbour.location_code ?? 'Next'}
              </button>
            ))
          : null}

        {/* Placement crosshair */}
        {placing ? (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <div className="rounded bg-black/70 px-3 py-1.5 text-xs text-white">
              <Crosshair className="mr-1 inline size-3.5" aria-hidden />
              Click the panorama to place the annotation
            </div>
          </div>
        ) : null}

        {loadState === 'loading' ? (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/45">
            <Loader2 className="size-6 animate-spin text-white/80" aria-label="Loading panorama" />
          </div>
        ) : null}
        {loadState === 'preview' ? (
          <div className="pointer-events-none absolute right-3 top-3 rounded bg-black/60 px-2 py-1 text-2xs text-white/85">
            Loading full resolution…
          </div>
        ) : null}

        {/* Controls */}
        {interactive ? (
          <div className="absolute bottom-3 right-3 flex items-center gap-1.5">
            <CompassRose yaw={orientation.yaw} />
            <div className="flex items-center gap-1 rounded bg-black/60 p-1 backdrop-blur">
              {onPlaceAnnotation ? (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => setPlacing((value) => !value)}
                  aria-label="Add annotation"
                  title="Add annotation"
                  className={cn('text-white hover:bg-white/15', placing && 'bg-white/25')}
                >
                  <MessageSquarePlus aria-hidden />
                </Button>
              ) : null}
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={() => canvasRef.current?.resetView()}
                aria-label="Reset view"
                title="Reset view"
                className="text-white hover:bg-white/15"
              >
                <RotateCcw aria-hidden />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={() => canvasRef.current?.requestFullscreen()}
                aria-label="Fullscreen"
                title="Fullscreen"
                className="text-white hover:bg-white/15"
              >
                <Expand aria-hidden />
              </Button>
            </div>
          </div>
        ) : null}

        {/* Orientation readout — a site engineer describing a defect needs the bearing. */}
        <div className="pointer-events-none absolute bottom-3 left-3 rounded bg-black/55 px-2 py-1 font-mono text-2xs text-white/85 backdrop-blur">
          <span className="tnum">{normaliseYaw(orientation.yaw).toFixed(0).padStart(3, '0')}°</span>
          <span className="mx-1.5 text-white/40">|</span>
          <span className="tnum">
            {orientation.pitch >= 0 ? '+' : ''}
            {orientation.pitch.toFixed(0)}°
          </span>
          <span className="mx-1.5 text-white/40">|</span>
          <span className="tnum">{orientation.fov.toFixed(0)}° fov</span>
        </div>
      </div>

      {manifest.history.length > 1 && onSelectFrame ? (
        <HistoryStrip manifest={manifest} onSelectFrame={onSelectFrame} />
      ) : null}
    </div>
  )
}

// ---- header -----------------------------------------------------------------
function ViewerHeader({ frame }: { frame: CaptureFrameDetail }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-b border-line px-3 py-2">
      <div className="min-w-0">
        <p className="truncate text-sm font-semibold text-ink">
          {frame.project_name ?? 'Project'}
          <span className="mx-1.5 text-ink-subtle">/</span>
          {frame.floor_name ?? 'Floor'}
          {frame.zone_name ? (
            <>
              <span className="mx-1.5 text-ink-subtle">/</span>
              {frame.zone_name}
            </>
          ) : null}
        </p>
        <p className="text-2xs text-ink-muted">{formatDateTime(frame.captured_at)}</p>
      </div>

      <div className="ml-auto flex flex-wrap items-center gap-2">
        {frame.location_code ? (
          <Badge tone="brand" icon={MapPin}>
            {frame.location_code}
          </Badge>
        ) : (
          <Badge tone="neutral">Not placed on plan</Badge>
        )}
        <QualityBadge verdict={frame.quality?.verdict} />
        {frame.position_provenance ? (
          <ProvenanceTag
            provenance={frame.position_provenance}
            confidence={frame.position_confidence}
          />
        ) : null}
        <span className="tnum text-2xs text-ink-subtle">
          <Ruler className="mr-0.5 inline size-3" aria-hidden />
          {frame.width_px}×{frame.height_px}
        </span>
      </div>
    </div>
  )
}

// ---- compass ----------------------------------------------------------------
/**
 * §11 asks for a compass "if available". It is honest about what it shows: the panorama's own
 * frame of reference, not magnetic north, because the platform has no device heading for an
 * uploaded image. The label says "rel" so nobody navigates by it.
 */
function CompassRose({ yaw }: { yaw: number }) {
  return (
    <div
      className="relative size-9 rounded-full bg-black/60 backdrop-blur"
      title="Viewing direction, relative to the panorama's own forward axis (not magnetic north)"
    >
      <Compass className="absolute inset-0 m-auto size-7 text-white/30" aria-hidden />
      <div
        className="absolute inset-0 m-auto flex items-start justify-center"
        style={{ transform: `rotate(${-normaliseYaw(yaw)}deg)` }}
      >
        <div className="mt-0.5 h-3 w-0.5 rounded-full bg-brand" />
      </div>
      <span className="absolute -bottom-3.5 left-1/2 -translate-x-1/2 text-[9px] uppercase text-white/60">
        rel
      </span>
    </div>
  )
}

function normaliseYaw(yaw: number): number {
  return ((yaw % 360) + 360) % 360
}

// ---- history strip ----------------------------------------------------------
/** Same location, other dates — the capture-history timeline inside the viewer (§16, §17). */
function HistoryStrip({
  manifest,
  onSelectFrame,
}: {
  manifest: ViewerManifest
  onSelectFrame: (frameId: string) => void
}) {
  return (
    <div className="border-t border-line bg-surface-raised/60 px-3 py-2">
      <div className="mb-1.5 flex items-center gap-2">
        <p className="label">
          History at {manifest.frame.location_code ?? 'this location'}
        </p>
        <span className="text-2xs text-ink-subtle">{manifest.history.length} visits</span>
      </div>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {manifest.history.map((entry) => {
          const active = entry.frame_id === manifest.frame.id
          return (
            <button
              key={entry.frame_id}
              type="button"
              onClick={() => onSelectFrame(entry.frame_id)}
              className={cn(
                'group shrink-0 overflow-hidden rounded border text-left transition-colors',
                active ? 'border-brand ring-1 ring-brand' : 'border-line hover:border-ink-subtle',
              )}
              style={{ width: 96 }}
            >
              {entry.thumbnail_url ? (
                <img
                  src={entry.thumbnail_url}
                  alt=""
                  loading="lazy"
                  className="h-12 w-full object-cover"
                />
              ) : (
                <div className="h-12 w-full bg-surface" />
              )}
              <div className="px-1.5 py-1">
                <p className="truncate text-2xs font-medium text-ink">
                  {formatDateTime(entry.captured_at)}
                </p>
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ---- filmstrip --------------------------------------------------------------
/**
 * The §11 capture strip: `P01 | P02 | P03 …` along the bottom of the viewer page.
 *
 * Virtualised by slicing a window around the active frame rather than rendering every thumbnail:
 * a video capture can produce 600 frames, and 600 `<img>` elements is a stalled tab (§35).
 */
export function FrameFilmstrip({
  frames,
  activeFrameId,
  onSelect,
  className,
}: {
  frames: CaptureFrameDetail[]
  activeFrameId: string
  onSelect: (frameId: string) => void
  className?: string
}) {
  const WINDOW = 40
  const activeIndex = Math.max(
    0,
    frames.findIndex((frame) => frame.id === activeFrameId),
  )
  const start = Math.max(0, activeIndex - WINDOW / 2)
  const visible = frames.slice(start, start + WINDOW)

  return (
    <div className={cn('panel px-3 py-2', className)}>
      <div className="mb-1.5 flex items-center justify-between">
        <p className="label">Capture positions</p>
        <span className="tnum text-2xs text-ink-subtle">
          {activeIndex + 1} of {frames.length}
          {frames.length > WINDOW ? ' (windowed)' : ''}
        </span>
      </div>
      <div className="flex gap-1.5 overflow-x-auto pb-1">
        {visible.map((frame) => {
          const active = frame.id === activeFrameId
          return (
            <button
              key={frame.id}
              type="button"
              onClick={() => onSelect(frame.id)}
              title={`${frame.location_code ?? `Frame ${frame.sequence_index + 1}`} — ${formatDateTime(frame.captured_at)}`}
              className={cn(
                'shrink-0 overflow-hidden rounded border transition-colors',
                active ? 'border-brand ring-1 ring-brand' : 'border-line hover:border-ink-subtle',
              )}
              style={{ width: 72 }}
            >
              {frame.thumbnail?.url ? (
                <img
                  src={frame.thumbnail.url}
                  alt=""
                  loading="lazy"
                  className="h-10 w-full object-cover"
                />
              ) : (
                <div className="h-10 w-full bg-surface-raised" />
              )}
              <div
                className={cn(
                  'truncate px-1 py-0.5 text-center text-[10px] font-semibold',
                  active ? 'bg-brand text-brand-ink' : 'text-ink-muted',
                )}
              >
                {frame.location_code ?? `#${frame.sequence_index + 1}`}
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ---- quality detail ---------------------------------------------------------
/** The §10 quality breakdown, shown beside the viewer with every sub-score named. */
export function QualityPanel({ frame }: { frame: CaptureFrameDetail }) {
  const quality = frame.quality
  if (!quality) {
    return (
      <Callout tone="info" title="Not scored">
        This frame has not been through the quality stage yet.
      </Callout>
    )
  }

  const scores: Array<[string, number]> = [
    ['Overall', quality.quality_score],
    ['Sharpness', quality.blur_score],
    ['Exposure', quality.brightness_score],
    ['Contrast', quality.contrast_score],
    ['Resolution', quality.resolution_score],
    ['Completeness', quality.completeness_score],
  ]

  return (
    <div className="space-y-2.5">
      <div className="flex items-center justify-between">
        <QualityBadge verdict={quality.verdict} />
        <ProvenanceTag provenance="measured" />
      </div>

      <dl className="space-y-1.5">
        {scores.map(([label, value]) => (
          <div key={label} className="flex items-center gap-2">
            <dt className="w-24 shrink-0 text-2xs text-ink-muted">{label}</dt>
            <dd className="flex min-w-0 flex-1 items-center gap-2">
              <div className="h-1 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-raised">
                <div
                  className={cn(
                    'h-full rounded-full',
                    value >= 0.75 ? 'bg-good' : value >= 0.5 ? 'bg-warn' : 'bg-bad',
                  )}
                  style={{ width: `${Math.round(value * 100)}%` }}
                />
              </div>
              <span className="tnum w-8 text-right text-2xs text-ink-muted">
                {formatPercent(value)}
              </span>
            </dd>
          </div>
        ))}
      </dl>

      {quality.warnings.length ? (
        <Callout tone="warn" title={`${quality.warnings.length} warning(s)`}>
          <ul className="list-inside list-disc space-y-0.5">
            {quality.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Callout>
      ) : null}

      {quality.is_duplicate ? (
        <Callout tone="info" title="Near-duplicate">
          The camera barely moved between this frame and the previous one, so it is excluded from
          automatic mapping.
        </Callout>
      ) : null}
    </div>
  )
}
