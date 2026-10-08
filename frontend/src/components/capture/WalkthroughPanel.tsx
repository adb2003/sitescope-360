/**
 * "This upload looks like three walkthroughs" — and what to do about it (§9, §46).
 *
 * Detection runs during mapping and proposes boundaries. This panel is where a person decides.
 * It cannot be a one-click action, because the one thing the detector cannot know is the thing
 * the split needs: which floor each walk was on. Nothing in the imagery says so, so the operator
 * says so, and until they do the panel is a report rather than a prompt.
 *
 * Two deliberate choices about what is shown:
 *
 * The **evidence** is on screen next to every boundary — how long the camera stopped, how much
 * the frames either side had in common, and whether the imagery could answer at all. A proposal
 * an operator cannot check is one they cannot refuse, and the failure mode this guards against
 * is a confident-looking split down the middle of a corridor.
 *
 * The **breaks that were considered and declined** are shown too, muted, under their own heading.
 * They are how an operator finds a boundary the detector missed: a five-minute pause that the
 * imagery says was a chat with a foreman is exactly where somebody who was there might disagree.
 */

import { AlertTriangle, Check, GitBranch, Scissors } from 'lucide-react'
import { useMemo, useState } from 'react'

import { Badge, Button, Callout, Card, CardHeader, Field, Select } from '@/components/ui'
import { useCaptureSegments, useProjectFloors, useSplitCapture } from '@/hooks/queries'
import { Capability } from '@/hooks/useAuth'
import { useProjectCan } from '@/hooks/useProjectCan'
import { ApiError } from '@/lib/api'
import type { CaptureSessionDetail, WalkthroughBoundary } from '@/types/api'

/** Segment index (1-based) → the floor the operator has put it on. */
type FloorChoice = Record<number, string>

export function WalkthroughPanel({ session }: { session: CaptureSessionDetail }) {
  // Scoped to the capture's own project: this panel is reached from a capture, and the
  // account may hold `capture:create` on other jobs and not this one.
  const { can } = useProjectCan(session.project_id)
  const report = useCaptureSegments(session.id)
  const floors = useProjectFloors(session.project_id)
  const split = useSplitCapture(session.id)
  const [chosen, setChosen] = useState<FloorChoice>({})
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const data = report.data
  const segments = useMemo(() => data?.segments ?? [], [data])
  const considered = (data?.boundaries ?? []).filter((boundary) => !boundary.proposed)

  // Nothing to offer: either the capture has not been processed, or it is one walk. Both are
  // ordinary, and neither deserves a panel telling the operator so.
  if (!data?.analysed || segments.length < 2) return null

  const submit = () => {
    setError(null)
    split.mutate(
      {
        segments: segments.map((segment) => ({
          start_sequence_index: segment.start_sequence_index,
          end_sequence_index: segment.end_sequence_index,
          // The first walkthrough stays in this capture and keeps its floor: its frames are
          // stored under it, and the split re-points rows rather than rewriting storage keys.
          floor_id: segment.index === 1 ? null : (chosen[segment.index] ?? null),
        })),
        reprocess: true,
      },
      {
        onSuccess: (result) => setDone(result.note),
        onError: (caught) =>
          setError(caught instanceof ApiError ? caught.message : 'Could not split this capture'),
      },
    )
  }

  return (
    <Card>
      <CardHeader
        title={`This upload looks like ${segments.length} walkthroughs`}
        description={data.note}
        actions={
          <Badge tone="warn" icon={GitBranch}>
            {data.proposed_count === 1 ? '1 break' : `${data.proposed_count} breaks`}
          </Badge>
        }
      />
      <div className="space-y-4 p-4">
        {error ? <Callout tone="bad">{error}</Callout> : null}
        {done ? (
          <Callout tone="info" title="Split">
            {done}
          </Callout>
        ) : null}

        <ol className="space-y-2">
          {segments.map((segment) => (
            <li
              key={segment.index}
              className="flex flex-wrap items-end justify-between gap-3 rounded border border-line bg-surface-raised p-3"
            >
              <div>
                <p className="text-sm font-medium text-ink">Walkthrough {segment.index}</p>
                <p className="text-xs text-ink-subtle">
                  {segment.frame_count} frames — positions {segment.start_sequence_index}–
                  {segment.end_sequence_index}
                </p>
              </div>
              {segment.index === 1 ? (
                <p className="max-w-xs text-xs text-ink-subtle">
                  Stays in this capture on {session.floor_name ?? 'its current floor'} — its
                  frames are stored under that floor.
                </p>
              ) : (
                <Field label="Which floor was this walked on?" className="w-56">
                  <Select
                    value={chosen[segment.index] ?? ''}
                    disabled={split.isPending || Boolean(done)}
                    onChange={(event) =>
                      setChosen((previous) => ({
                        ...previous,
                        [segment.index]: event.target.value,
                      }))
                    }
                  >
                    {/*
                      The capture's own floor is the empty option, not a listed one. Offering it
                      twice — once as "unchanged" and again by name — makes an operator choose
                      between two spellings of the same thing.
                    */}
                    <option value="">{session.floor_name ?? 'Same floor'} (unchanged)</option>
                    {(floors.data ?? [])
                      .filter((floor) => floor.id !== session.floor_id)
                      .map((floor) => (
                        <option key={floor.id} value={floor.id}>
                          {floor.name}
                        </option>
                      ))}
                  </Select>
                </Field>
              )}
            </li>
          ))}
        </ol>

        <section className="space-y-2">
          <h3 className="text-xs font-medium uppercase tracking-wide text-ink-muted">
            Why these breaks
          </h3>
          {(data.boundaries ?? [])
            .filter((boundary) => boundary.proposed)
            .map((boundary) => (
              <Evidence key={boundary.link_index} boundary={boundary} />
            ))}
        </section>

        {considered.length ? (
          <section className="space-y-2">
            <h3 className="text-xs font-medium uppercase tracking-wide text-ink-muted">
              Also examined, and not split on
            </h3>
            {considered.map((boundary) => (
              <Evidence key={boundary.link_index} boundary={boundary} muted />
            ))}
          </section>
        ) : null}

        <Callout tone="warn" title="Splitting clears the estimated track">
          A track estimated straight through a break was not measuring anything, so it is
          discarded from every resulting walkthrough and each one is mapped again on its own.
          Frame quality and features are kept — those are facts about each frame.
        </Callout>

        {can(Capability.CaptureCreate) && !done ? (
          <Button variant="primary" icon={Scissors} loading={split.isPending} onClick={submit}>
            Split into {segments.length} walkthroughs
          </Button>
        ) : null}
      </div>
    </Card>
  )
}

function Evidence({ boundary, muted }: { boundary: WalkthroughBoundary; muted?: boolean }) {
  return (
    <div
      className={
        'rounded border border-line p-3 text-xs ' + (muted ? 'text-ink-subtle' : 'text-ink-muted')
      }
    >
      <div className="mb-1 flex flex-wrap items-center gap-2">
        {boundary.scene_break === true ? (
          <Badge tone="bad" icon={AlertTriangle}>
            The view changed
          </Badge>
        ) : boundary.scene_break === false ? (
          <Badge tone="good" icon={Check}>
            The view continued
          </Badge>
        ) : (
          // Not "the view continued" and not "it changed". An honest third answer, because two
          // frames with no texture cannot support either.
          <Badge tone="warn">Could not tell from the imagery</Badge>
        )}
        {boundary.time_break ? <Badge tone="warn">Camera stopped</Badge> : null}
        <span className="font-mono">{Math.round(boundary.confidence * 100)}% confidence</span>
      </div>
      <p>{boundary.reason}</p>
    </div>
  )
}
