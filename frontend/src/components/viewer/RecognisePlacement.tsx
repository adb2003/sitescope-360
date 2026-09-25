/**
 * Placing a walkthrough by recognising a floor that has been captured before (§15, §46).
 *
 * Anchoring by hand is the right answer exactly once per floor. After that the floor is covered
 * in frames whose plan positions are known, and this asks the server whether it recognises where
 * the new walk was taken — a frame photographed from the spot an earlier frame was photographed
 * from differs from it only by rotation, and that is measurable.
 *
 * Three things are shown rather than a bare success, because an automatic placement has to be
 * arguable:
 *
 * - **which mode it used** — the whole track from a fitted transform, or only the frames actually
 *   recognised, which is a much weaker and much more honest result;
 * - **how many frames were recognised**, and how many landed on the earlier capture's own points,
 *   since sharing a point is what makes two dates comparable at all;
 * - **the worst parallax among the matches**, which is the evidence. Near zero means the cameras
 *   were in nearly the same place; it is a tolerance, not proof of identity, so the positions
 *   stay labelled estimated and are worth a glance on the plan.
 *
 * A refusal is not an error state here. Most floors have a first capture, and "nothing to
 * recognise this against" is the correct answer for it.
 */

import { ScanSearch } from 'lucide-react'

import { Button, Callout } from '@/components/ui'
import type { LocalizeResult } from '@/types/api'

interface RecognisePlacementProps {
  result: LocalizeResult | null
  pending: boolean
  error: string | null
  onRun: () => void
}

/** The largest residual parallax among the accepted matches — the weakest link in the evidence. */
function worstParallax(result: LocalizeResult): number | null {
  if (result.matches.length === 0) return null
  return Math.max(...result.matches.map((match) => match.parallax_deg))
}

/**
 * Why nothing was placed, said as the specific thing it was.
 *
 * `mode: none` covers situations that call for different responses — anchor this walk by hand,
 * or leave a placement alone that someone may have corrected — so the title branches on `reason`
 * rather than flattening all of them into "nothing recognised", which is untrue of most of them.
 */
const REFUSAL_TITLE: Record<LocalizeResult['reason'], string> = {
  placed: 'Placed',
  no_references: 'No earlier capture to recognise this against',
  no_matches: 'Nothing recognised',
  already_placed: 'This capture is already placed',
  not_processed: 'Not processed yet',
}

function Outcome({ result }: { result: LocalizeResult }) {
  if (result.mode === 'none') {
    return (
      <Callout tone="info" title={REFUSAL_TITLE[result.reason]}>
        <span className="block">{result.note}</span>
        {result.references_considered > 0 ? (
          <span className="mt-1 block text-ink-subtle">
            Compared against {result.references_considered} placed frame
            {result.references_considered === 1 ? '' : 's'} from earlier captures of this floor.
          </span>
        ) : null}
      </Callout>
    )
  }

  const parallax = worstParallax(result)
  const partial = result.mode === 'recognised'

  return (
    <Callout
      tone={partial ? 'warn' : 'info'}
      title={
        partial
          ? `Placed ${result.placed_frames} recognised frame${result.placed_frames === 1 ? '' : 's'}`
          : `Placed ${result.placed_frames} frame${result.placed_frames === 1 ? '' : 's'} automatically`
      }
    >
      <span className="block">
        {result.matched_frames} frame{result.matched_frames === 1 ? '' : 's'} recognised against
        earlier captures; {result.reused_locations} joined a point an earlier capture already
        uses.
      </span>
      {partial ? (
        <span className="mt-1 block">
          Too few to fit a transform over the whole walk, so only the recognised frames were
          placed — the rest still need anchoring by hand.
        </span>
      ) : result.rms_error_px !== null ? (
        <span className="mt-1 block">
          Recognised frames landed {result.rms_error_px.toFixed(1)} px from where the fit put them.
        </span>
      ) : null}
      {parallax !== null ? (
        <span className="tnum mt-1 block text-ink-subtle">
          Worst residual parallax {parallax.toFixed(2)}° — the cameras were close, not identical.
          Positions are estimated; check them on the plan.
        </span>
      ) : null}
    </Callout>
  )
}

export function RecognisePlacement({ result, pending, error, onRun }: RecognisePlacementProps) {
  return (
    <div className="space-y-3">
      <div>
        <p className="text-xs font-medium text-ink">Place from earlier captures</p>
        <p className="mt-0.5 text-xs text-ink-subtle">
          If this floor has been walked before, the frames it recognises can place this walk
          without anchors. Runs automatically after processing; this re-runs it.
        </p>
      </div>

      <Button size="sm" variant="secondary" icon={ScanSearch} loading={pending} onClick={onRun}>
        Recognise this walk
      </Button>

      {error ? <Callout tone="bad">{error}</Callout> : null}
      {result ? <Outcome result={result} /> : null}
    </div>
  )
}
