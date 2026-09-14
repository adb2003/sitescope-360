/**
 * What was captured near the point the operator clicked, on every date.
 *
 * The question a construction viewer exists to answer is not "what is here" but "what was here
 * in June" — and the click that asks it lands on the drawing, not on a capture point. So this is
 * driven by a plan coordinate and a radius rather than by a location id, and "nothing was
 * captured here" is a real answer it has to be able to give.
 *
 * The minimap keeps resolving clicks to the current walkthrough itself, instantly and without a
 * round trip. This is the other half: the dates that walkthrough is one of.
 */

import { Camera, History, MapPin } from 'lucide-react'

import { Callout, Spinner } from '@/components/ui'
import type { CapturesNearPoint } from '@/types/api'
import { cn, formatDateTime, formatRelative } from '@/lib/utils'

interface TimeTravelPanelProps {
  /** Null until the operator has clicked somewhere. */
  point: { x: number; y: number } | null
  results: CapturesNearPoint[] | undefined
  loading: boolean
  radiusPx: number
  activeFrameId: string | null
  onSelectFrame: (frameId: string) => void
  onCompare?: (frameA: string, frameB: string) => void
}

export function TimeTravelPanel({
  point,
  results,
  loading,
  radiusPx,
  activeFrameId,
  onSelectFrame,
  onCompare,
}: TimeTravelPanelProps) {
  if (!point) {
    return (
      <p className="text-xs text-ink-subtle">
        Click anywhere on the floor plan to see what was captured there, across every visit.
      </p>
    )
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-ink-subtle">
        <Spinner className="size-3.5" /> Looking for captures near that point…
      </div>
    )
  }

  if (!results?.length) {
    return (
      <Callout tone="info" title="Nothing captured here">
        No capture point lies within {Math.round(radiusPx)} px of where you clicked. That is a
        gap in coverage, not a failure — nobody has walked this part of the floor.
      </Callout>
    )
  }

  return (
    <div className="space-y-3">
      {results.map((result) => (
        <div key={result.location.id} className="rounded border border-line">
          <div className="flex items-baseline justify-between gap-2 border-b border-line px-2.5 py-1.5">
            <span className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-ink">
              <MapPin className="size-3.5 shrink-0 text-brand" aria-hidden />
              <span className="truncate">{result.location.code}</span>
              {result.location.label ? (
                <span className="truncate text-ink-subtle">· {result.location.label}</span>
              ) : null}
            </span>
            {/* The distance is what separates a point they hit from one they merely came
                closest to, so it is shown rather than implied. */}
            <span className="tnum shrink-0 text-[11px] text-ink-subtle">
              {result.distance_px < 1 ? '<1' : Math.round(result.distance_px)} px away
            </span>
          </div>

          {result.visits.length ? (
            <ul className="divide-y divide-line">
              {result.visits.map((visit, index) => {
                const isActive = visit.frame_id === activeFrameId
                return (
                  <li key={visit.frame_id}>
                    <button
                      type="button"
                      onClick={() => onSelectFrame(visit.frame_id)}
                      className={cn(
                        'flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-xs transition-colors',
                        isActive ? 'bg-brand/10 text-ink' : 'hover:bg-surface-raised',
                      )}
                    >
                      {visit.thumbnail?.url ? (
                        <img
                          src={visit.thumbnail.url}
                          alt=""
                          loading="lazy"
                          className="size-9 shrink-0 rounded object-cover"
                        />
                      ) : (
                        <span className="grid size-9 shrink-0 place-items-center rounded bg-surface-raised">
                          <Camera className="size-3.5 text-ink-subtle" aria-hidden />
                        </span>
                      )}
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-ink">
                          {formatDateTime(visit.captured_at)}
                        </span>
                        <span className="block truncate text-[11px] text-ink-subtle">
                          {formatRelative(visit.captured_at)}
                          {index === 0 ? ' · latest' : ''}
                        </span>
                      </span>
                      {isActive ? (
                        <span className="shrink-0 text-[10px] font-medium uppercase tracking-wide text-brand">
                          viewing
                        </span>
                      ) : null}
                    </button>
                  </li>
                )
              })}
            </ul>
          ) : (
            <p className="px-2.5 py-2 text-xs text-ink-subtle">
              This point exists on the plan but nothing has been bound to it yet.
            </p>
          )}

          {/* Two visits is the minimum for a comparison to mean anything. */}
          {onCompare && result.visits.length > 1 ? (
            <div className="border-t border-line px-2.5 py-1.5">
              <button
                type="button"
                className="flex items-center gap-1.5 text-xs text-brand hover:underline"
                onClick={() =>
                  onCompare(result.visits[0]!.frame_id, result.visits[1]!.frame_id)
                }
              >
                <History className="size-3.5" aria-hidden />
                Compare the two most recent
              </button>
            </div>
          ) : null}
        </div>
      ))}
    </div>
  )
}
