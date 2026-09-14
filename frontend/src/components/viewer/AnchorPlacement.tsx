/**
 * Placing a whole walkthrough on the floor plan from two manual anchors (§15).
 *
 * The estimator recovers the *shape* of the walk and nothing else — monocular geometry has no
 * scale, no compass bearing and no origin. Those three arrive together the moment the operator
 * says "this frame was taken here" about two frames: a similarity transform is fitted to the
 * pairs and applied to every other frame, so a thirty-frame walkthrough is placed by two clicks
 * instead of thirty.
 *
 * The leverage is the point and also the risk. One misplaced anchor moves every frame, and the
 * result still looks like a measurement — so this shows the residual the server reports back
 * rather than a bare success, and a fit the server refuses is surfaced with its reason instead of
 * being retried quietly.
 */

import { Crosshair, MapPin, Trash2 } from 'lucide-react'

import { Button, Callout } from '@/components/ui'
import { anchorsAreClose, type AnchorPoint } from './anchorSpan'
import type { AlignTrackResult, CaptureFrame } from '@/types/api'
import { formatDateTime } from '@/lib/utils'

/** An anchor the operator has placed but not yet submitted. */
export type PendingAnchor = AnchorPoint

interface AnchorPlacementProps {
  frames: CaptureFrame[]
  activeFrameId: string
  anchors: PendingAnchor[]
  picking: boolean
  pending: boolean
  result: AlignTrackResult | null
  /** How many anchors produced `result`. Decides whether its residual means anything. */
  anchorsUsed: number | null
  error: string | null
  onTogglePicking: () => void
  onRemove: (frameId: string) => void
  onSubmit: () => void
}

/** Where a frame sits in the walkthrough, for naming it without a code it does not have yet. */
function describeFrame(frames: CaptureFrame[], frameId: string): string {
  const index = frames.findIndex((frame) => frame.id === frameId)
  if (index < 0) return 'Unknown frame'
  const frame = frames[index]!
  return `Frame ${index + 1} of ${frames.length} · ${formatDateTime(frame.captured_at)}`
}

export function AnchorPlacement({
  frames,
  activeFrameId,
  anchors,
  picking,
  pending,
  result,
  anchorsUsed,
  error,
  onTogglePicking,
  onRemove,
  onSubmit,
}: AnchorPlacementProps) {
  const alreadyAnchored = anchors.some((anchor) => anchor.frameId === activeFrameId)
  const enough = anchors.length >= 2

  return (
    <div className="space-y-3">
      <div>
        <p className="text-xs font-medium text-ink">Place this walkthrough from anchors</p>
        <p className="mt-0.5 text-xs text-ink-subtle">
          The estimated track has a shape but no position, scale or bearing. Say where two frames
          were taken and the rest place themselves.
        </p>
      </div>

      <ol className="space-y-1.5">
        {anchors.map((anchor, index) => (
          <li
            key={anchor.frameId}
            className="flex items-center gap-2 rounded border border-line px-2 py-1.5 text-xs"
          >
            <span className="grid size-5 shrink-0 place-items-center rounded-full border border-dashed border-estimated text-[10px] font-bold text-estimated">
              {index + 1}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-ink">
                {describeFrame(frames, anchor.frameId)}
              </span>
              <span className="tnum block text-[11px] text-ink-subtle">
                at ({anchor.x}, {anchor.y}) on the plan
              </span>
            </span>
            <Button
              size="sm"
              variant="ghost"
              icon={Trash2}
              aria-label="Remove this anchor"
              onClick={() => onRemove(anchor.frameId)}
            />
          </li>
        ))}
      </ol>

      {anchors.length < 2 ? (
        <p className="text-xs text-ink-subtle">
          {anchors.length === 0
            ? 'Open the frame you can recognise on the plan, then drop its anchor.'
            : 'Now open a different frame — ideally far from the first — and anchor that one too.'}
        </p>
      ) : null}

      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant={picking ? 'primary' : 'secondary'}
          icon={picking ? MapPin : Crosshair}
          onClick={onTogglePicking}
          disabled={pending}
        >
          {picking
            ? 'Click the plan…'
            : alreadyAnchored
              ? 'Move this frame’s anchor'
              : 'Anchor the current frame'}
        </Button>
        <Button size="sm" variant="primary" loading={pending} disabled={!enough} onClick={onSubmit}>
          Place {frames.length} frame{frames.length === 1 ? '' : 's'}
        </Button>
      </div>

      {/* Two anchors on nearly the same spot leave scale barely determined, so the fit is
          arithmetically valid and practically useless. Worth saying before the submit, not after. */}
      {enough && anchorsAreClose(anchors) ? (
        <Callout tone="warn" title="Anchors are very close together">
          Scale and rotation come from the distance and direction between them, so two nearby
          anchors give a fit that is extremely sensitive to a few pixels of error. Pick points at
          opposite ends of the walk if you can.
        </Callout>
      ) : null}

      {error ? <Callout tone="bad">{error}</Callout> : null}

      {result ? (
        <Callout
          tone={result.rms_error_px > 20 ? 'warn' : 'info'}
          title={`Placed ${result.placed_frames} frame${result.placed_frames === 1 ? '' : 's'}`}
        >
          <span className="block">
            The track was scaled ×{result.scale.toFixed(2)} and rotated{' '}
            {result.rotation_deg.toFixed(1)}°.
          </span>
          {/*
            With exactly two anchors the residual is zero by construction and measures nothing: a
            similarity transform has four degrees of freedom and two correspondences supply four
            constraints, so it reproduces them exactly however wrong they were. Printing "0.0 px"
            beside a success reads as "perfect", which is the opposite of what it means — so the
            number is only shown once a third anchor makes it capable of disagreeing.
          */}
          {anchorsUsed !== null && anchorsUsed <= 2 ? (
            <span className="mt-1 block">
              Two anchors fix the transform exactly, so there is no residual to check it against.
              Add a third and re-place if you want the fit tested — and check the positions on the
              plan either way.
            </span>
          ) : (
            <span className="mt-1 block">
              Anchors landed {result.rms_error_px.toFixed(1)} px from where they were dropped.
            </span>
          )}
          <span className="mt-1 block text-ink-subtle">{result.note}</span>
        </Callout>
      ) : null}
    </div>
  )
}
