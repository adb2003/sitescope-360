/**
 * What the operator is told after automatic placement runs.
 *
 * The server answers every ordinary refusal with a 200 and `mode: none`, which is right — a
 * floor nobody has captured before is not an error. The risk lives entirely on this side: if the
 * UI flattens those into one message, an operator reads "nothing recognised" about a capture that
 * was simply already placed, and concludes the feature is broken when it declined on purpose.
 *
 * The other thing asserted here is that a partial result is never dressed up as a whole one.
 * `recognised` means only the matched frames were placed and the rest of the walk still needs
 * anchoring; presenting that as a placed capture would leave frames unplaced with nothing saying
 * so.
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { RecognisePlacement } from './RecognisePlacement'
import type { LocalizeMatch, LocalizeResult } from '@/types/api'

function match(overrides: Partial<LocalizeMatch> = {}): LocalizeMatch {
  return {
    query_index: 0,
    reference_frame_id: 'F-old',
    reference_session_id: 'CAP-EARLIER',
    location_id: 'L-1',
    plan_x: 100,
    plan_y: 100,
    inlier_count: 420,
    parallax_deg: 0.9,
    confidence: 0.8,
    ...overrides,
  }
}

function result(overrides: Partial<LocalizeResult> = {}): LocalizeResult {
  return {
    ok: true,
    mode: 'transform',
    reason: 'placed',
    note: 'Placed automatically.',
    placed_frames: 5,
    matched_frames: 5,
    reused_locations: 5,
    new_locations: 0,
    references_considered: 40,
    generation: 1,
    rms_error_px: 3.5,
    matches: [match(), match({ query_index: 1, parallax_deg: 2.4 })],
    ...overrides,
  }
}

function show(value: LocalizeResult | null) {
  render(
    <RecognisePlacement result={value} pending={false} error={null} onRun={vi.fn()} />,
  )
}

describe('RecognisePlacement', () => {
  it('says what was placed and how much of it joined earlier points', () => {
    show(result())

    expect(screen.getByText(/Placed 5 frames automatically/)).toBeInTheDocument()
    expect(screen.getByText(/5 joined a point an earlier capture already uses/)).toBeInTheDocument()
  })

  it('shows the worst parallax, not the best', () => {
    // The evidence is only as good as its weakest match, and an average would hide the one match
    // that was nearly out of tolerance.
    show(result())

    expect(screen.getByText(/Worst residual parallax 2\.40°/)).toBeInTheDocument()
  })

  it('never calls a partial placement a placed walk', () => {
    show(
      result({
        mode: 'recognised',
        placed_frames: 2,
        matched_frames: 2,
        reused_locations: 2,
        rms_error_px: null,
      }),
    )

    expect(screen.getByText(/Placed 2 recognised frames/)).toBeInTheDocument()
    expect(screen.getByText(/the rest still need anchoring by hand/)).toBeInTheDocument()
  })

  it.each([
    ['no_references', 'No earlier capture to recognise this against'],
    ['no_matches', 'Nothing recognised'],
    ['already_placed', 'This capture is already placed'],
    ['not_processed', 'Not processed yet'],
  ] as const)('tells %s apart from the other refusals', (reason, title) => {
    show(result({ ok: false, mode: 'none', reason, placed_frames: 0, matches: [] }))

    expect(screen.getByText(title)).toBeInTheDocument()
  })

  it('warns when the placement rests on a chain of earlier estimates', () => {
    // Generation four looks exactly like generation zero in the coordinates, which is the whole
    // reason it has to be said in words.
    show(result({ generation: 4 }))

    expect(screen.getByText(/4 hops of automatic recognition/)).toBeInTheDocument()
  })

  it('does not mention hops for a placement anchored to hand-placed points', () => {
    show(result({ generation: 1 }))

    expect(screen.queryByText(/hops of automatic recognition/)).not.toBeInTheDocument()
  })

  it('says nothing at all before it has been run', () => {
    show(null)

    expect(screen.getByRole('button', { name: /Recognise this walk/ })).toBeInTheDocument()
    expect(screen.queryByText(/parallax/)).not.toBeInTheDocument()
  })
})
