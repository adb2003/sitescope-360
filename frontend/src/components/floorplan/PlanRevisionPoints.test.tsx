/**
 * A capture point belongs to the plan revision it was placed on (§12, §13).
 *
 * Coordinates are stored in the **pixels of one raster**, and a floor can hold several plan
 * revisions — choosing a different sheet of a drawing set creates one, precisely so that
 * existing coordinates are not silently reinterpreted against a different drawing. The editor
 * then went ahead and reinterpreted them anyway: it drew every point on the floor over whichever
 * plan was active, so a point placed on the general arrangement appeared on the section, at a
 * position describing neither.
 *
 * Two halves matter equally here, and the second is the one that makes this a fix rather than a
 * different bug. Points from another revision must not be **drawn**; and the operator must be
 * **told** they exist, because somebody who placed P01 last month and cannot find it will
 * conclude it was deleted.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import {
  FloorPlanCanvas,
  LocationList,
  partitionByPlan,
} from '@/components/floorplan/FloorPlanCanvas'
import type { CaptureLocation, FloorPlan } from '@/types/api'

const PLAN = {
  id: 'fp-sheet-3',
  floor_id: 'f1',
  name: "Architect's set — sheet 3",
  content_type: 'image/png',
  width_px: 900,
  height_px: 580,
  file_size_bytes: 1234,
  px_per_meter: 40,
  calibration: null,
  render: null,
  is_active: true,
  created_at: '2026-09-20T00:00:00Z',
  asset: { key: 'k', url: 'http://test/plan.png', expires_in: 3600 },
} as unknown as FloorPlan

function point(overrides: Partial<CaptureLocation> = {}): CaptureLocation {
  return {
    id: 'l1',
    floor_id: 'f1',
    zone_id: null,
    floor_plan_id: PLAN.id,
    code: 'P01',
    label: 'North wing',
    x: 100,
    y: 100,
    provenance: 'manual',
    confidence: null,
    placement_generation: 0,
    created_at: '2026-09-01T00:00:00Z',
    frame_count: 2,
    latest_frame_id: 'fr1',
    latest_captured_at: '2026-09-10T09:00:00Z',
    ...overrides,
  } as CaptureLocation
}

describe('partitionByPlan', () => {
  it('keeps the points placed on this revision', () => {
    const { onPlan, elsewhere } = partitionByPlan([point()], PLAN.id)

    expect(onPlan).toHaveLength(1)
    expect(elsewhere).toHaveLength(0)
  })

  it('sets aside the points placed on another revision', () => {
    const { onPlan, elsewhere } = partitionByPlan(
      [point({ id: 'old', floor_plan_id: 'fp-sheet-1' })],
      PLAN.id,
    )

    expect(onPlan).toHaveLength(0)
    expect(elsewhere.map((p) => p.id)).toEqual(['old'])
  })

  it('keeps a point whose revision is unknown', () => {
    // Null is "we do not know which revision", which is not the same as knowing it was another
    // one — and on a floor with a single plan it is the ordinary case. Hiding known-good data
    // on the strength of a guess is the worse of the two failures.
    const { onPlan, elsewhere } = partitionByPlan([point({ floor_plan_id: null })], PLAN.id)

    expect(onPlan).toHaveLength(1)
    expect(elsewhere).toHaveLength(0)
  })
})

describe('the plan editor', () => {
  const mixed = [
    point({ id: 'here', code: 'P01' }),
    point({ id: 'old-a', code: 'P07', floor_plan_id: 'fp-sheet-1', x: 400, y: 300 }),
    point({ id: 'old-b', code: 'P08', floor_plan_id: 'fp-sheet-1', x: 500, y: 310 }),
  ]

  it('draws only the points belonging to the plan on screen', () => {
    render(<FloorPlanCanvas plan={PLAN} locations={mixed} />)

    expect(screen.getByTitle(/P01/)).toBeInTheDocument()
    // These two carry coordinates in sheet 1's raster. Drawing them here would put them
    // somewhere that means nothing, and a reader cannot tell a misplaced pin from a real one.
    expect(screen.queryByTitle(/P07/)).not.toBeInTheDocument()
    expect(screen.queryByTitle(/P08/)).not.toBeInTheDocument()
  })

  it('says how many it withheld, rather than hiding them silently', () => {
    render(<FloorPlanCanvas plan={PLAN} locations={mixed} />)

    expect(screen.getByText(/2 point\(s\) not shown/)).toBeInTheDocument()
    expect(screen.getByText(/earlier revision of this floor plan/)).toBeInTheDocument()
  })

  it('says nothing when every point belongs to this plan', () => {
    // The notice must be absent in the ordinary case, or it becomes furniture people stop
    // reading — which is how the real warning gets missed.
    render(<FloorPlanCanvas plan={PLAN} locations={[point()]} />)

    expect(screen.queryByText(/not shown/)).not.toBeInTheDocument()
  })

  it('cannot select a point it did not draw', async () => {
    const onSelect = vi.fn()
    render(<FloorPlanCanvas plan={PLAN} locations={mixed} onSelectLocation={onSelect} />)

    await userEvent.click(screen.getByTitle(/P01/))

    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ code: 'P01' }))
    expect(onSelect).not.toHaveBeenCalledWith(expect.objectContaining({ code: 'P07' }))
  })
})

describe('the point list', () => {
  const mixed = [
    point({ id: 'here', code: 'P01', label: 'North wing' }),
    point({ id: 'old', code: 'P07', label: 'Plant room', floor_plan_id: 'fp-sheet-1' }),
  ]

  it('still lists a point from an earlier revision, and marks it', () => {
    // Listed, because its captures are real and worth opening. Marked, because selecting it
    // highlights a pin the canvas is deliberately not drawing.
    render(<LocationList locations={mixed} onSelect={() => {}} activePlanId={PLAN.id} />)

    expect(screen.getByText('Plant room')).toBeInTheDocument()
    expect(screen.getByText('on an earlier sheet')).toBeInTheDocument()
  })

  it('marks nothing when it has not been told which plan is on screen', () => {
    // Without an active plan there is nothing to compare against, so claiming a point is on an
    // earlier sheet would be a guess.
    render(<LocationList locations={mixed} onSelect={() => {}} />)

    expect(screen.queryByText('on an earlier sheet')).not.toBeInTheDocument()
  })
})
