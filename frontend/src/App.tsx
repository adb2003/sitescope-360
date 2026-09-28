import { Suspense, lazy } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { Spinner } from '@/components/ui'
import { LoginPage } from '@/pages/LoginPage'
import { SharedViewPage } from '@/pages/SharedViewPage'
import { PortfolioPage } from '@/pages/PortfolioPage'
import { ProjectsPage } from '@/pages/ProjectsPage'
import { ProjectDashboardPage } from '@/pages/ProjectDashboardPage'

// Split the heavy routes. The 360 viewer and the compare view both pull in three.js, and most
// sessions open the dashboard and nothing else (§35).
const ViewerPage = lazy(() => import('@/pages/ViewerPage').then((m) => ({ default: m.ViewerPage })))
const ComparePage = lazy(() =>
  import('@/pages/ComparePage').then((m) => ({ default: m.ComparePage })),
)
const FloorPlansPage = lazy(() =>
  import('@/pages/FloorPlansPage').then((m) => ({ default: m.FloorPlansPage })),
)
const CapturePage = lazy(() =>
  import('@/pages/CapturePage').then((m) => ({ default: m.CapturePage })),
)
const CaptureDetailPage = lazy(() =>
  import('@/pages/CaptureDetailPage').then((m) => ({ default: m.CaptureDetailPage })),
)
const TimelinePage = lazy(() =>
  import('@/pages/TimelinePage').then((m) => ({ default: m.TimelinePage })),
)
const AnnotationsPage = lazy(() =>
  import('@/pages/AnnotationsPage').then((m) => ({ default: m.AnnotationsPage })),
)
const ReportsPage = lazy(() =>
  import('@/pages/ReportsPage').then((m) => ({ default: m.ReportsPage })),
)
const SettingsPage = lazy(() =>
  import('@/pages/SettingsPage').then((m) => ({ default: m.SettingsPage })),
)

function RouteFallback() {
  return (
    <div className="flex h-64 items-center justify-center gap-2 text-sm text-ink-muted">
      <Spinner />
      Loading…
    </div>
  )
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      {/*
        Outside AppShell, and outside the auth guard. A visitor holding a share link has no
        account and no project — a page wrapped in the application's sidebar would offer them
        navigation they cannot use and read as a failed sign-in.
      */}
      <Route path="/s/:token" element={<SharedViewPage />} />

      <Route element={<AppShell />}>
        <Route index element={<PortfolioPage />} />
        <Route path="projects" element={<ProjectsPage />} />
        <Route path="projects/:projectId" element={<ProjectDashboardPage />} />

        <Route
          path="floorplans"
          element={
            <Suspense fallback={<RouteFallback />}>
              <FloorPlansPage />
            </Suspense>
          }
        />
        <Route
          path="viewer"
          element={
            <Suspense fallback={<RouteFallback />}>
              <ViewerPage />
            </Suspense>
          }
        />
        <Route
          path="viewer/:frameId"
          element={
            <Suspense fallback={<RouteFallback />}>
              <ViewerPage />
            </Suspense>
          }
        />
        <Route
          path="compare"
          element={
            <Suspense fallback={<RouteFallback />}>
              <ComparePage />
            </Suspense>
          }
        />
        <Route
          path="capture"
          element={
            <Suspense fallback={<RouteFallback />}>
              <CapturePage />
            </Suspense>
          }
        />
        <Route
          path="capture/:sessionId"
          element={
            <Suspense fallback={<RouteFallback />}>
              <CaptureDetailPage />
            </Suspense>
          }
        />
        <Route
          path="timeline"
          element={
            <Suspense fallback={<RouteFallback />}>
              <TimelinePage />
            </Suspense>
          }
        />
        <Route
          path="annotations"
          element={
            <Suspense fallback={<RouteFallback />}>
              <AnnotationsPage />
            </Suspense>
          }
        />
        <Route
          path="reports"
          element={
            <Suspense fallback={<RouteFallback />}>
              <ReportsPage />
            </Suspense>
          }
        />
        <Route
          path="settings"
          element={
            <Suspense fallback={<RouteFallback />}>
              <SettingsPage />
            </Suspense>
          }
        />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
