/**
 * Page-level tests (§39): login, project creation, capture upload, viewer, timeline, floor plan.
 *
 * Rendered against a mocked `fetch` returning the API's real response shapes. Two things this
 * catches that a unit test would not: the §46 honesty guarantees actually reaching the screen
 * (an estimated position rendered as "Estimated", an absent number as "—"), and a role without a
 * capability not being offered the button.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import type { ReactElement } from 'react'

import { AuthProvider } from '@/hooks/useAuth'
import { ProjectContextProvider } from '@/hooks/useProjectContext'
import { LoginPage } from '@/pages/LoginPage'
import { PortfolioPage } from '@/pages/PortfolioPage'
import { TimelinePage } from '@/pages/TimelinePage'
import { FloorPlanCanvas } from '@/components/floorplan/FloorPlanCanvas'
import { Metric, ProvenanceTag } from '@/components/status'
import { tokens } from '@/lib/api'

// ---- harness ----------------------------------------------------------------
let fetchMock: Mock

/** Route requests by URL so a page's parallel queries can all be satisfied. */
function mockApi(routes: Record<string, unknown>) {
  fetchMock.mockImplementation((input: string | URL) => {
    const url = String(input)
    for (const [pattern, body] of Object.entries(routes)) {
      if (url.includes(pattern)) {
        return Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
        )
      }
    }
    return Promise.resolve(
      new Response(JSON.stringify({ code: 'not_found', message: `unmocked: ${url}` }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      }),
    )
  })
}

function renderPage(ui: ReactElement, { route = '/' }: { route?: string } = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <ProjectContextProvider>
          <AuthProvider>{ui}</AuthProvider>
        </ProjectContextProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const SESSION = {
  user: {
    id: 'u1',
    email: 'admin@test.example.com',
    full_name: 'Test Admin',
    role: 'admin',
    is_active: true,
    must_change_password: false,
    last_login_at: null,
    created_at: '2026-09-01T00:00:00Z',
  },
  capabilities: ['project:read', 'project:write', 'capture:read', 'upload', 'location:edit'],
}

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  tokens.clear()
  localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ---- login ------------------------------------------------------------------
describe('LoginPage', () => {
  it('signs in and stores both tokens', async () => {
    mockApi({
      '/auth/login': {
        access_token: 'access-1',
        refresh_token: 'refresh-1',
        token_type: 'bearer',
        expires_at: '2026-09-10T12:00:00Z',
      },
      '/auth/me': SESSION,
    })

    renderPage(<LoginPage />)

    await userEvent.type(screen.getByLabelText(/email/i), 'admin@test.example.com')
    await userEvent.type(screen.getByLabelText(/password/i), 'test-password-1234')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))

    await waitFor(() => expect(tokens.access).toBe('access-1'))
    expect(tokens.refresh).toBe('refresh-1')
  })

  it('shows the server message on a bad password', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ code: 'authentication_failed', message: 'Incorrect email or password' }),
        { status: 401, headers: { 'content-type': 'application/json' } },
      ),
    )

    renderPage(<LoginPage />)
    await userEvent.type(screen.getByLabelText(/email/i), 'admin@test.example.com')
    await userEvent.type(screen.getByLabelText(/password/i), 'wrong')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByText(/incorrect email or password/i)).toBeInTheDocument()
  })

  it('states the independence claim on the sign-in screen', () => {
    mockApi({})
    renderPage(<LoginPage />)
    expect(screen.getByText(/uses no OpenSpace/i)).toBeInTheDocument()
  })
})

// ---- portfolio --------------------------------------------------------------
describe('PortfolioPage', () => {
  const PORTFOLIO = {
    projects: [
      {
        project_id: 'p1',
        project_name: 'Chevron Phase 2',
        project_code: 'CVX-P2',
        status: 'active',
        floor_count: 3,
        session_count: 4,
        coverage_ratio: 0.82,
        last_capture_at: '2026-09-09T09:00:00Z',
        frequency_status: 'ok',
        open_annotations: 2,
      },
      {
        project_id: 'p2',
        project_name: 'Riverside Warehouse',
        project_code: 'RVS-1',
        status: 'active',
        floor_count: 1,
        session_count: 0,
        coverage_ratio: 0,
        last_capture_at: null,
        frequency_status: 'never_captured',
        open_annotations: 0,
      },
    ],
    total_projects: 2,
    total_sessions: 4,
    total_frames: 84,
    overdue_count: 0,
  }

  beforeEach(() => {
    tokens.set('access-1', 'refresh-1')
  })

  it('renders the portfolio table with coverage', async () => {
    mockApi({ '/auth/me': SESSION, '/api/dashboard': PORTFOLIO, '/projects': { items: [], total: 0, limit: 50, offset: 0 } })

    renderPage(<PortfolioPage />)

    expect(await screen.findByText('Chevron Phase 2')).toBeInTheDocument()
    expect(screen.getByText('CVX-P2')).toBeInTheDocument()
    expect(screen.getByText('82%')).toBeInTheDocument()
    expect(screen.getByText(/never captured/i)).toBeInTheDocument()
  })

  it('states that coverage is declared rather than detected', async () => {
    mockApi({ '/auth/me': SESSION, '/api/dashboard': PORTFOLIO, '/projects': { items: [], total: 0, limit: 50, offset: 0 } })
    renderPage(<PortfolioPage />)
    expect(
      await screen.findByText(/administrator-declared capture points, not inferred from imagery/i),
    ).toBeInTheDocument()
  })

  it('names the browser tab after the page, not after the product', async () => {
    /*
      Every route used to set the same title. Two jobs open side by side — the comparison this
      product exists for — gave two tabs reading "SiteScope 360", a history menu of one repeated
      word, and nothing for a screen reader to announce on navigation.

      Asserted through a real page rather than on the helper alone, because the wiring is the
      part that breaks: the title is set by `PageHeader`, so that it and the `h1` are the same
      string and cannot drift.
    */
    mockApi({ '/auth/me': SESSION, '/api/dashboard': PORTFOLIO, '/projects': { items: [], total: 0, limit: 50, offset: 0 } })

    renderPage(<PortfolioPage />)

    await screen.findByText('Chevron Phase 2')
    expect(document.title).toBe('Dashboard · SiteScope 360')
  })

  it('declares up front that progress and defect AI are absent', async () => {
    mockApi({ '/auth/me': SESSION, '/api/dashboard': PORTFOLIO, '/projects': { items: [], total: 0, limit: 50, offset: 0 } })
    renderPage(<PortfolioPage />)
    expect(await screen.findByText(/No progress or defect AI/i)).toBeInTheDocument()
  })
})

// ---- timeline ---------------------------------------------------------------
describe('TimelinePage', () => {
  beforeEach(() => {
    tokens.set('access-1', 'refresh-1')
    localStorage.setItem('sitescope.project_id', 'p1')
  })

  it('renders the §16 bar chart and names the denominator', async () => {
    mockApi({
      '/auth/me': SESSION,
      '/projects/p1/timeline': {
        project_id: 'p1',
        floor_id: null,
        location_total: 12,
        note: null,
        entries: [
          {
            capture_date: '2026-09-10',
            session_ids: ['CAP-1'],
            session_count: 1,
            frame_count: 21,
            located_frame_count: 11,
            coverage_ratio: 0.92,
            coverage_provenance: 'measured',
            floors: ['Ground'],
            operators: ['Sam'],
          },
          {
            capture_date: '2026-09-07',
            session_ids: ['CAP-2'],
            session_count: 1,
            frame_count: 18,
            located_frame_count: 9,
            coverage_ratio: 0.72,
            coverage_provenance: 'measured',
            floors: ['Ground'],
            operators: ['Sam'],
          },
        ],
      },
      '/projects/p1/frequency': {
        project_id: 'p1',
        project_name: 'Demo',
        floor_id: null,
        floor_name: null,
        expected_per_week: 2,
        last_capture_at: '2026-09-10T09:15:00Z',
        days_since_last_capture: 0,
        next_expected_date: '2026-09-13',
        status: 'ok',
        captures_this_week: 1,
      },
      '/projects/p1/floors': [],
      '/api/captures': { items: [], total: 0, limit: 100, offset: 0 },
    })

    renderPage(<TimelinePage />)

    expect(await screen.findByText('92%')).toBeInTheDocument()
    expect(screen.getByText('72%')).toBeInTheDocument()
    // The denominator must be visible, or a percentage means nothing.
    expect(
      screen.getByText(/share of 12 known capture point\(s\) visited on that date/i),
    ).toBeInTheDocument()
  })
})

// ---- floor plan -------------------------------------------------------------
describe('FloorPlanCanvas', () => {
  const PLAN = {
    id: 'fp1',
    floor_id: 'f1',
    name: 'Ground GA',
    content_type: 'image/png',
    width_px: 900,
    height_px: 580,
    file_size_bytes: 12345,
    px_per_meter: 40,
    calibration: null,
    render: null,
    is_active: true,
    created_at: '2026-09-01T00:00:00Z',
    asset: { key: 'k', url: 'http://test/plan.png', expires_in: 3600 },
  }

  const LOCATIONS = [
    {
      id: 'l1',
      floor_id: 'f1',
      zone_id: null,
      floor_plan_id: 'fp1',
      code: 'P01',
      label: 'North wing',
      x: 100,
      y: 100,
      provenance: 'manual' as const,
      confidence: null,
      // Zero hops from a person: someone dropped this pin.
      placement_generation: 0,
      created_at: '2026-09-01T00:00:00Z',
      frame_count: 2,
      latest_frame_id: 'fr1',
      latest_captured_at: '2026-09-10T09:00:00Z',
    },
    {
      id: 'l2',
      floor_id: 'f1',
      zone_id: null,
      floor_plan_id: 'fp1',
      code: 'P02',
      label: null,
      x: 200,
      y: 150,
      provenance: 'estimated' as const,
      confidence: 0.61,
      // Estimated, but anchored against points a person placed — one hop, not a chain.
      placement_generation: 1,
      created_at: '2026-09-01T00:00:00Z',
      frame_count: 0,
      latest_frame_id: null,
      latest_captured_at: null,
    },
  ]

  it('renders a pin per capture point and opens one on click', async () => {
    const onSelect = vi.fn()
    render(
      <FloorPlanCanvas plan={PLAN} locations={LOCATIONS} onSelectLocation={onSelect} />,
    )

    // Pins show the numeric part of the code.
    const pin = screen.getByTitle(/P01/)
    expect(pin).toBeInTheDocument()
    await userEvent.click(pin)
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ code: 'P01' }))
  })

  it('distinguishes visited from never-captured points in the legend', () => {
    render(<FloorPlanCanvas plan={PLAN} locations={LOCATIONS} />)
    expect(screen.getByText('Captured')).toBeInTheDocument()
    expect(screen.getByText('Never captured')).toBeInTheDocument()
    expect(screen.getByText('Estimated position')).toBeInTheDocument()
    // A never-captured point must say so, not show a zero.
    expect(screen.getByTitle(/P02.*Never captured/)).toBeInTheDocument()
  })

  it('refuses placement on a plan with no raster dimensions', () => {
    // PDFs are rasterised on upload now, so this is no longer the everyday case — but a plan
    // stored before that existed still has zero dimensions, and placing points against a
    // zero-size grid divides by zero and pins everything to one corner.
    render(
      <FloorPlanCanvas
        plan={{ ...PLAN, width_px: 0, height_px: 0, calibration: { note: 'Legacy plan' } }}
        locations={[]}
      />,
    )
    expect(screen.getByText(/cannot be used for placement/i)).toBeInTheDocument()
    expect(screen.getByText(/Legacy plan/)).toBeInTheDocument()
  })

  it('draws an estimated path dashed and a manual path solid', () => {
    const { container } = render(
      <FloorPlanCanvas
        plan={PLAN}
        locations={LOCATIONS}
        paths={[
          {
            id: 'path1',
            session_id: 'CAP-1',
            floor_id: 'f1',
            provenance: 'estimated',
            confidence: 0.6,
            steps: [
              { location_id: 'l1', frame_id: 'fr1', code: 'P01', x: 100, y: 100, timestamp: '2026-09-10T09:00:00Z' },
              { location_id: 'l2', frame_id: 'fr2', code: 'P02', x: 200, y: 150, timestamp: '2026-09-10T09:01:00Z' },
            ],
          },
        ]}
      />,
    )

    // Scoped to the path polyline: Lucide icons in the toolbar also render <polyline>.
    const polyline = container.querySelector('polyline[data-capture-path]')
    expect(polyline).not.toBeNull()
    // Dashed marks the path as estimated rather than known (§46).
    expect(polyline?.getAttribute('stroke-dasharray')).toBeTruthy()
  })
})

// ---- honesty primitives -----------------------------------------------------
describe('§46 display guarantees', () => {
  it('renders an absent value as an em dash, never as zero', () => {
    render(<Metric label="Coverage" value={null} />)
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.queryByText('0%')).toBeNull()
  })

  it('labels an estimate as estimated, with its confidence', () => {
    render(<ProvenanceTag provenance="estimated" confidence={0.61} />)
    expect(screen.getByText('Estimated')).toBeInTheDocument()
    expect(screen.getByText('61%')).toBeInTheDocument()
  })

  it('labels a measurement as measured', () => {
    render(<ProvenanceTag provenance="measured" />)
    expect(screen.getByText('Measured')).toBeInTheDocument()
  })

  it('renders nothing when provenance is unknown rather than guessing', () => {
    const { container } = render(<ProvenanceTag provenance={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows an unscored frame as not scored, not as good', async () => {
    const { QualityBadge } = await import('@/components/status')
    render(<QualityBadge verdict={null} />)
    expect(screen.getByText('Not scored')).toBeInTheDocument()
  })

  it('renders the backend mapping note verbatim rather than paraphrasing it', async () => {
    const { MappingConfidenceNote } = await import('@/components/status')
    const note =
      'Automatic mapping confidence: Low (37%) — manual placement recommended. 1 of 5 frame pairs could not be related geometrically.'
    render(<MappingConfidenceNote confidence="low" note={note} />)
    expect(screen.getByText(note)).toBeInTheDocument()
  })

  it('hatches an estimated progress bar so it is distinguishable at a glance', async () => {
    const { Progress } = await import('@/components/ui')
    const { container } = render(<Progress value={0.5} estimated />)
    expect(container.querySelector('.estimated-hatch')).not.toBeNull()
  })
})

// ---- capability gating ------------------------------------------------------
describe('capability gating', () => {
  it('hides the create-project action from a role without project:write', async () => {
    tokens.set('access-1', 'refresh-1')
    mockApi({
      '/auth/me': {
        ...SESSION,
        user: { ...SESSION.user, role: 'viewer' },
        capabilities: ['project:read', 'capture:read'],
      },
      '/api/dashboard': {
        projects: [],
        total_projects: 0,
        total_sessions: 0,
        total_frames: 0,
        overdue_count: 0,
      },
      '/projects': { items: [], total: 0, limit: 50, offset: 0 },
    })

    const { ProjectsPage } = await import('@/pages/ProjectsPage')
    renderPage(<ProjectsPage />)

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Projects' })).toBeInTheDocument())
    const header = screen.getByRole('heading', { name: 'Projects' }).closest('div')?.parentElement
    expect(within(header as HTMLElement).queryByRole('button', { name: /create project/i })).toBeNull()
  })
})
