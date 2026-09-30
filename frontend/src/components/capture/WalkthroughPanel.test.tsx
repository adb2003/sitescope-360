/**
 * The panel where a person decides whether one upload was really one walk.
 *
 * Three things have to reach the screen and all three are easy to lose quietly.
 *
 * The panel must be **absent** when there is nothing to decide — a capture that has not been
 * processed, or one that really was a single walk. A permanent "0 breaks found" card trains an
 * operator to ignore the place the real answer will appear.
 *
 * "Could not tell" must never render as "the view continued". They are different answers: one
 * says the imagery disagreed with the clock, the other says the imagery abstained, and only the
 * first is a reason to leave a walkthrough joined.
 *
 * And the operator's floor must reach the segment that moves — that choice is the whole reason
 * the split cannot be automatic.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import { WalkthroughPanel } from './WalkthroughPanel'
import { AuthProvider } from '@/hooks/useAuth'
import { tokens } from '@/lib/api'
import type { CaptureSessionDetail, WalkthroughBoundary, WalkthroughReport } from '@/types/api'

let fetchMock: Mock

const SESSION = {
  user: {
    id: 'u1',
    email: 'admin@test.example.com',
    full_name: 'Test Admin',
    role: 'admin',
    is_active: true,
    last_login_at: null,
    created_at: '2026-09-01T00:00:00Z',
  },
  capabilities: ['capture:read', 'capture:create', 'upload'],
}

const CAPTURE = {
  id: 'CAP-20260929-080000-MORN',
  project_id: 'p-1',
  floor_id: 'f-0',
  floor_name: 'Level 00',
  name: "Tuesday's walk",
} as unknown as CaptureSessionDetail

function boundary(overrides: Partial<WalkthroughBoundary> = {}): WalkthroughBoundary {
  return {
    link_index: 4,
    after_frame_id: 'fr-4',
    before_frame_id: 'fr-5',
    after_sequence_index: 4,
    breaks_at: '2026-09-29T09:14:00Z',
    gap_seconds: 180,
    cadence_seconds: 2,
    gap_ratio: 90,
    inliers: 3,
    matches: 11,
    time_break: true,
    scene_break: true,
    proposed: true,
    confidence: 0.92,
    reason: 'The camera stopped for 180s. The two frames share nothing.',
    ...overrides,
  }
}

function report(overrides: Partial<WalkthroughReport> = {}): WalkthroughReport {
  return {
    session_id: CAPTURE.id,
    analysed: true,
    cadence_seconds: 2,
    proposed_count: 1,
    note: 'This upload looks like 2 separate walkthroughs (5, 5 frames).',
    segments: [
      { index: 1, start_sequence_index: 0, end_sequence_index: 4, frame_count: 5 },
      { index: 2, start_sequence_index: 5, end_sequence_index: 9, frame_count: 5 },
    ],
    boundaries: [boundary()],
    ...overrides,
  }
}

const FLOORS = [
  { id: 'f-0', name: 'Level 00' },
  { id: 'f-1', name: 'Level 01' },
]

const SPLIT_RESULT = {
  parent_id: CAPTURE.id,
  created: [],
  moved_frames: 5,
  cleared_track: true,
  reprocessing: [],
  note: 'Split into 2 walkthroughs.',
}

function bodyFor(url: string): unknown {
  if (url.includes('/split')) return SPLIT_RESULT
  if (url.includes('/segments')) return null // set per test
  if (url.includes('/floors')) return FLOORS
  if (url.includes('/auth/me')) return SESSION
  return undefined
}

function show(segments: WalkthroughReport) {
  fetchMock.mockImplementation((input: string | URL) => {
    const url = String(input)
    const body = url.includes('/segments') ? segments : bodyFor(url)
    if (body === undefined) {
      return Promise.resolve(
        new Response(JSON.stringify({ message: `unmocked: ${url}` }), { status: 404 }),
      )
    }
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
  })
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <WalkthroughPanel session={CAPTURE} />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  tokens.set('access-token', 'refresh-token')
})

afterEach(() => {
  vi.unstubAllGlobals()
  tokens.clear()
})

describe('WalkthroughPanel', () => {
  it('offers the split when the upload looks like several walks', async () => {
    show(report())

    expect(await screen.findByText(/looks like 2 walkthroughs/i)).toBeInTheDocument()
    expect(screen.getByText(/5 frames — positions 0–4/)).toBeInTheDocument()
    expect(screen.getByText(/The two frames share nothing/)).toBeInTheDocument()
  })

  it('says nothing at all when the capture is one walk', async () => {
    show(
      report({
        segments: [{ index: 1, start_sequence_index: 0, end_sequence_index: 9, frame_count: 10 }],
        boundaries: [],
        proposed_count: 0,
      }),
    )

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(screen.queryByText(/walkthrough/i)).not.toBeInTheDocument()
  })

  it('says nothing at all before the capture has been processed', async () => {
    show(report({ analysed: false, segments: [], boundaries: [] }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /split/i })).not.toBeInTheDocument()
  })

  it('does not report an abstention as agreement', async () => {
    // `scene_break: null` means the frames were too featureless to judge. Rendering that as
    // "the view continued" would claim evidence that does not exist.
    show(report({ boundaries: [boundary({ scene_break: null, inliers: 0 })] }))

    expect(await screen.findByText(/Could not tell from the imagery/)).toBeInTheDocument()
    expect(screen.queryByText(/The view continued/)).not.toBeInTheDocument()
  })

  it('shows the breaks it examined and declined, under their own heading', async () => {
    show(
      report({
        boundaries: [
          boundary(),
          boundary({
            link_index: 8,
            proposed: false,
            scene_break: false,
            reason:
              'The imagery continues across it — 140 verified matches — so the walk did too.',
          }),
        ],
      }),
    )

    expect(await screen.findByText(/Also examined, and not split on/)).toBeInTheDocument()
    expect(screen.getByText(/so the walk did too/)).toBeInTheDocument()
  })

  // Note what this does *not* prove: that a floor chosen for the first segment would be
  // suppressed. It cannot, because the panel offers no way to choose one — which is the point,
  // and the API refusing it outright is tested on the backend
  // (`test_the_first_walkthrough_cannot_be_moved_to_another_floor`).
  it("sends the operator's floor for the segment that moves, and none for the one that stays", async () => {
    show(report())
    await screen.findByText(/looks like 2 walkthroughs/i)

    await userEvent.selectOptions(screen.getByRole('combobox'), 'f-1')
    await userEvent.click(screen.getByRole('button', { name: /Split into 2 walkthroughs/ }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => String(url).includes('/split'))
      expect(call).toBeTruthy()
      const body = JSON.parse(String((call?.[1] as RequestInit).body))
      expect(body.segments[0].floor_id).toBeNull()
      expect(body.segments[1].floor_id).toBe('f-1')
    })
  })

  it('offers only one floor selector, because only one segment can move', async () => {
    show(report())

    await screen.findByText(/looks like 2 walkthroughs/i)
    expect(screen.getAllByRole('combobox')).toHaveLength(1)
    expect(screen.getByText(/Stays in this capture on Level 00/)).toBeInTheDocument()
  })
})
