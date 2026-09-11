/**
 * TanStack Query hooks — one per endpoint the UI uses.
 *
 * Two things are centralised here on purpose:
 *
 * - **Query keys.** Written once, so an invalidation after a mutation cannot miss a cache entry
 *   because a component spelled the key differently.
 * - **Polling policy.** A capture being processed needs a live status; a project list does not.
 *   `useProcessingStatus` polls only while work is in flight and stops the moment the pipeline
 *   settles, so an idle dashboard makes no requests.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query'

import { api, type QueryValue } from '@/lib/api'
import { isSessionInFlight } from '@/components/status'
import type {
  Annotation,
  Building,
  CameraDevice,
  CameraProviderInfo,
  CaptureFileDetail,
  CaptureFrameDetail,
  CapturePath,
  CaptureSchedule,
  CaptureSession,
  CaptureSessionDetail,
  CaptureLocation,
  Coverage,
  FloorDetail,
  FloorPlan,
  FrameComparison,
  FrequencyStatus,
  JobSummary,
  Message,
  Page,
  Portfolio,
  ProcessingJob,
  ProcessingStatus,
  Project,
  ProjectDashboard,
  ProjectSummary,
  Report,
  ReportCatalogue,
  StaleAreas,
  SystemCapabilities,
  Timeline,
  User,
  ViewerManifest,
  Zone,
} from '@/types/api'

export const keys = {
  portfolio: ['portfolio'] as const,
  capabilities: ['system', 'capabilities'] as const,
  projects: (filters?: Record<string, QueryValue>) => ['projects', filters ?? {}] as const,
  project: (id: string) => ['project', id] as const,
  dashboard: (id: string) => ['project', id, 'dashboard'] as const,
  coverage: (id: string) => ['project', id, 'coverage'] as const,
  timeline: (id: string, floorId?: string | null) =>
    ['project', id, 'timeline', floorId ?? 'all'] as const,
  frequency: (id: string, floorId?: string | null) =>
    ['project', id, 'frequency', floorId ?? 'all'] as const,
  staleAreas: (id: string, days: number) => ['project', id, 'stale', days] as const,
  buildings: (projectId: string) => ['project', projectId, 'buildings'] as const,
  projectFloors: (projectId: string) => ['project', projectId, 'floors'] as const,
  floors: (buildingId: string) => ['building', buildingId, 'floors'] as const,
  floor: (id: string) => ['floor', id] as const,
  zones: (floorId: string) => ['floor', floorId, 'zones'] as const,
  floorPlan: (floorId: string) => ['floor', floorId, 'floorplan'] as const,
  floorPlans: (floorId: string) => ['floor', floorId, 'floorplans'] as const,
  locations: (floorId: string) => ['floor', floorId, 'locations'] as const,
  floorPaths: (floorId: string) => ['floor', floorId, 'paths'] as const,
  captures: (filters?: Record<string, QueryValue>) => ['captures', filters ?? {}] as const,
  capture: (id: string) => ['capture', id] as const,
  captureFiles: (id: string) => ['capture', id, 'files'] as const,
  captureFrames: (id: string, filters?: Record<string, QueryValue>) =>
    ['capture', id, 'frames', filters ?? {}] as const,
  captureStatus: (id: string) => ['capture', id, 'status'] as const,
  capturePath: (id: string) => ['capture', id, 'path'] as const,
  frame: (id: string) => ['frame', id] as const,
  viewer: (id: string) => ['frame', id, 'viewer'] as const,
  frameAnnotations: (id: string) => ['frame', id, 'annotations'] as const,
  locationFrames: (id: string) => ['location', id, 'frames'] as const,
  compareLocation: (id: string, a?: string, b?: string) =>
    ['compare', 'location', id, a ?? '', b ?? ''] as const,
  compareFrames: (a: string, b: string) => ['compare', 'frames', a, b] as const,
  annotations: (projectId: string, filters?: Record<string, QueryValue>) =>
    ['project', projectId, 'annotations', filters ?? {}] as const,
  reports: (projectId: string) => ['project', projectId, 'reports'] as const,
  reportTypes: ['report-types'] as const,
  schedules: (projectId: string) => ['project', projectId, 'schedule'] as const,
  jobs: (filters?: Record<string, QueryValue>) => ['jobs', filters ?? {}] as const,
  jobSummary: ['jobs', 'summary'] as const,
  cameras: ['cameras'] as const,
  cameraProviders: ['cameras', 'providers'] as const,
  users: ['users'] as const,
}

type Opts<T> = Omit<UseQueryOptions<T, Error, T>, 'queryKey' | 'queryFn'>

// ---- system -----------------------------------------------------------------
export function useCapabilities(options?: Opts<SystemCapabilities>) {
  return useQuery({
    queryKey: keys.capabilities,
    queryFn: () => api.get<SystemCapabilities>('/system/capabilities'),
    // Capabilities change only on deploy.
    staleTime: 30 * 60 * 1000,
    ...options,
  })
}

// ---- portfolio & projects ---------------------------------------------------
export function usePortfolio() {
  return useQuery({ queryKey: keys.portfolio, queryFn: () => api.get<Portfolio>('/dashboard') })
}

export function useProjects(filters: { q?: string; status?: string } = {}) {
  return useQuery({
    queryKey: keys.projects(filters),
    queryFn: () => api.get<Page<ProjectSummary>>('/projects', { ...filters, limit: 100 }),
  })
}

export function useProject(id: string | null | undefined) {
  return useQuery({
    queryKey: keys.project(id ?? ''),
    queryFn: () => api.get<ProjectSummary>(`/projects/${id}`),
    enabled: Boolean(id),
  })
}

export function useDashboard(projectId: string | null | undefined) {
  return useQuery({
    queryKey: keys.dashboard(projectId ?? ''),
    queryFn: () => api.get<ProjectDashboard>(`/projects/${projectId}/dashboard`),
    enabled: Boolean(projectId),
  })
}

export function useCoverage(projectId: string | null | undefined) {
  return useQuery({
    queryKey: keys.coverage(projectId ?? ''),
    queryFn: () => api.get<Coverage>(`/projects/${projectId}/coverage`),
    enabled: Boolean(projectId),
  })
}

export function useTimeline(projectId: string | null | undefined, floorId?: string | null) {
  return useQuery({
    queryKey: keys.timeline(projectId ?? '', floorId),
    queryFn: () =>
      api.get<Timeline>(`/projects/${projectId}/timeline`, { floor_id: floorId, limit: 120 }),
    enabled: Boolean(projectId),
  })
}

export function useFrequency(projectId: string | null | undefined, floorId?: string | null) {
  return useQuery({
    queryKey: keys.frequency(projectId ?? '', floorId),
    queryFn: () =>
      api.get<FrequencyStatus>(`/projects/${projectId}/frequency`, { floor_id: floorId }),
    enabled: Boolean(projectId),
  })
}

export function useStaleAreas(projectId: string | null | undefined, thresholdDays = 14) {
  return useQuery({
    queryKey: keys.staleAreas(projectId ?? '', thresholdDays),
    queryFn: () =>
      api.get<StaleAreas>(`/projects/${projectId}/stale-areas`, { threshold_days: thresholdDays }),
    enabled: Boolean(projectId),
  })
}

// ---- hierarchy --------------------------------------------------------------
export function useBuildings(projectId: string | null | undefined) {
  return useQuery({
    queryKey: keys.buildings(projectId ?? ''),
    queryFn: () => api.get<Building[]>(`/projects/${projectId}/buildings`),
    enabled: Boolean(projectId),
  })
}

export function useProjectFloors(projectId: string | null | undefined) {
  return useQuery({
    queryKey: keys.projectFloors(projectId ?? ''),
    queryFn: () => api.get<FloorDetail[]>(`/projects/${projectId}/floors`),
    enabled: Boolean(projectId),
  })
}

export function useFloor(floorId: string | null | undefined) {
  return useQuery({
    queryKey: keys.floor(floorId ?? ''),
    queryFn: () => api.get<FloorDetail>(`/floors/${floorId}`),
    enabled: Boolean(floorId),
  })
}

export function useZones(floorId: string | null | undefined) {
  return useQuery({
    queryKey: keys.zones(floorId ?? ''),
    queryFn: () => api.get<Zone[]>(`/floors/${floorId}/zones`),
    enabled: Boolean(floorId),
  })
}

export function useFloorPlan(floorId: string | null | undefined) {
  return useQuery({
    queryKey: keys.floorPlan(floorId ?? ''),
    queryFn: () => api.get<FloorPlan>(`/floors/${floorId}/floorplan`),
    enabled: Boolean(floorId),
    // A floor legitimately has no plan yet; a 404 is an answer, not a failure to retry.
    retry: false,
  })
}

export function useLocations(floorId: string | null | undefined) {
  return useQuery({
    queryKey: keys.locations(floorId ?? ''),
    queryFn: () => api.get<CaptureLocation[]>(`/floors/${floorId}/locations`),
    enabled: Boolean(floorId),
  })
}

export function useFloorPaths(floorId: string | null | undefined) {
  return useQuery({
    queryKey: keys.floorPaths(floorId ?? ''),
    queryFn: () => api.get<CapturePath[]>(`/floors/${floorId}/paths`),
    enabled: Boolean(floorId),
  })
}

// ---- captures ---------------------------------------------------------------
/**
 * The capture list, polled gently so work arriving from the field shows up on its own.
 *
 * Captures are created by the mobile app, not by this one: an operator finishes a walkthrough on
 * site and the office is looking at a list that will never mention it. `useProcessingStatus`
 * polls a session it already knows about, which does not help — the problem is learning that a
 * *new* session exists.
 *
 * 20 seconds, and React Query pauses the timer while the tab is unfocused, so an abandoned tab
 * costs nothing. Fast enough that "it should be there by now" is true within a coffee-length
 * pause; slow enough that a dashboard left open all day is not hammering the API.
 */
export function useCaptures(filters: Record<string, QueryValue> = {}) {
  return useQuery({
    queryKey: keys.captures(filters),
    queryFn: () => api.get<Page<CaptureSession>>('/captures', { limit: 100, ...filters }),
    refetchInterval: 20_000,
  })
}

export function useCapture(sessionId: string | null | undefined) {
  return useQuery({
    queryKey: keys.capture(sessionId ?? ''),
    queryFn: () => api.get<CaptureSessionDetail>(`/captures/${sessionId}`),
    enabled: Boolean(sessionId),
  })
}

export function useCaptureFiles(sessionId: string | null | undefined) {
  return useQuery({
    queryKey: keys.captureFiles(sessionId ?? ''),
    queryFn: () => api.get<CaptureFileDetail[]>(`/captures/${sessionId}/files`),
    enabled: Boolean(sessionId),
  })
}

export function useCaptureFrames(
  sessionId: string | null | undefined,
  filters: Record<string, QueryValue> = {},
) {
  return useQuery({
    queryKey: keys.captureFrames(sessionId ?? '', filters),
    queryFn: () =>
      api.get<Page<CaptureFrameDetail>>(`/captures/${sessionId}/frames`, {
        limit: 200,
        ...filters,
      }),
    enabled: Boolean(sessionId),
  })
}

/**
 * Live pipeline status, polled only while there is work to watch.
 *
 * `refetchInterval` returns `false` once the session reaches a terminal state, so an open
 * capture page stops making requests the moment processing finishes rather than polling forever.
 */
export function useProcessingStatus(sessionId: string | undefined, active = true) {
  return useQuery({
    queryKey: keys.captureStatus(sessionId ?? ''),
    queryFn: () => api.get<ProcessingStatus>(`/captures/${sessionId}/status`),
    enabled: Boolean(sessionId) && active,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (!status) return 1500
      if (status === 'ready' || status === 'failed' || status === 'cancelled') return false
      return isSessionInFlight(status) || status === 'uploaded' || status === 'draft' ? 1500 : 2500
    },
  })
}

export function useCapturePath(sessionId: string | null | undefined) {
  return useQuery({
    queryKey: keys.capturePath(sessionId ?? ''),
    queryFn: () => api.get<CapturePath>(`/captures/${sessionId}/path`),
    enabled: Boolean(sessionId),
    retry: false,
  })
}

// ---- viewer -----------------------------------------------------------------
export function useViewerManifest(frameId: string | null | undefined) {
  return useQuery({
    queryKey: keys.viewer(frameId ?? ''),
    queryFn: () => api.get<ViewerManifest>(`/frames/${frameId}/viewer`),
    enabled: Boolean(frameId),
  })
}

export function useLocationFrames(locationId: string | null | undefined) {
  return useQuery({
    queryKey: keys.locationFrames(locationId ?? ''),
    queryFn: () => api.get<CaptureFrameDetail[]>(`/locations/${locationId}/frames`),
    enabled: Boolean(locationId),
  })
}

export function useLocationComparison(
  locationId: string | null | undefined,
  dateA?: string,
  dateB?: string,
) {
  return useQuery({
    queryKey: keys.compareLocation(locationId ?? '', dateA, dateB),
    queryFn: () =>
      api.get<FrameComparison>(`/compare/locations/${locationId}`, {
        date_a: dateA,
        date_b: dateB,
      }),
    enabled: Boolean(locationId),
    retry: false,
  })
}

export function useFrameComparison(frameA: string | undefined, frameB: string | null | undefined) {
  return useQuery({
    queryKey: keys.compareFrames(frameA ?? '', frameB ?? ''),
    queryFn: () => api.get<FrameComparison>('/compare/frames', { frame_a: frameA, frame_b: frameB }),
    enabled: Boolean(frameA && frameB),
  })
}

// ---- annotations ------------------------------------------------------------
export function useFrameAnnotations(frameId: string | null | undefined) {
  return useQuery({
    queryKey: keys.frameAnnotations(frameId ?? ''),
    queryFn: () => api.get<Annotation[]>(`/frames/${frameId}/annotations`),
    enabled: Boolean(frameId),
  })
}

export function useProjectAnnotations(
  projectId: string | null | undefined,
  filters: Record<string, QueryValue> = {},
) {
  return useQuery({
    queryKey: keys.annotations(projectId ?? '', filters),
    queryFn: () =>
      api.get<Page<Annotation>>(`/projects/${projectId}/annotations`, { limit: 200, ...filters }),
    enabled: Boolean(projectId),
  })
}

// ---- reports ----------------------------------------------------------------
export function useReports(projectId: string | null | undefined) {
  return useQuery({
    queryKey: keys.reports(projectId ?? ''),
    queryFn: () => api.get<Page<Report>>(`/projects/${projectId}/reports`),
    enabled: Boolean(projectId),
  })
}

export function useReportTypes() {
  return useQuery({
    queryKey: keys.reportTypes,
    queryFn: () => api.get<ReportCatalogue>('/report-types'),
    staleTime: 30 * 60 * 1000,
  })
}

// ---- settings / admin -------------------------------------------------------
export function useSchedules(projectId: string | null | undefined) {
  return useQuery({
    queryKey: keys.schedules(projectId ?? ''),
    queryFn: () => api.get<CaptureSchedule[]>(`/projects/${projectId}/schedule`),
    enabled: Boolean(projectId),
  })
}

export function useJobs(filters: Record<string, QueryValue> = {}) {
  return useQuery({
    queryKey: keys.jobs(filters),
    queryFn: () => api.get<Page<ProcessingJob>>('/jobs', { limit: 100, ...filters }),
    refetchInterval: 5000,
  })
}

export function useJobSummary() {
  return useQuery({
    queryKey: keys.jobSummary,
    queryFn: () => api.get<JobSummary>('/jobs/summary'),
    refetchInterval: 10000,
  })
}

export function useCameraProviders() {
  return useQuery({
    queryKey: keys.cameraProviders,
    queryFn: () => api.get<CameraProviderInfo[]>('/cameras/providers'),
    staleTime: 30 * 60 * 1000,
  })
}

export function useCameras() {
  return useQuery({
    queryKey: keys.cameras,
    queryFn: () => api.get<CameraDevice[]>('/cameras', { active_only: false }),
  })
}

export function useUsers(enabled = true) {
  return useQuery({
    queryKey: keys.users,
    queryFn: () => api.get<Page<User>>('/users', { limit: 100 }),
    enabled,
  })
}

// ---- mutations --------------------------------------------------------------
export function useCreateProject() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: Record<string, unknown>) => api.post<Project>('/projects', body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
      void queryClient.invalidateQueries({ queryKey: keys.portfolio })
    },
  })
}

export function useCreateBuilding(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: { name: string; description?: string }) =>
      api.post<Building>(`/projects/${projectId}/buildings`, body),
    onSuccess: () => invalidateProject(queryClient, projectId),
  })
}

export function useCreateFloor(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ buildingId, ...body }: { buildingId: string; name: string; level: number; area_sqft?: number }) =>
      api.post<FloorDetail>(`/buildings/${buildingId}/floors`, body),
    onSuccess: () => invalidateProject(queryClient, projectId),
  })
}

export function useCreateZone(projectId: string, floorId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: { name: string; area_sqft?: number; expected_capture_points: number }) =>
      api.post<Zone>(`/floors/${floorId}/zones`, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.zones(floorId) })
      invalidateProject(queryClient, projectId)
    },
  })
}

export function useUploadFloorPlan(projectId: string, floorId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (file: File) => {
      const form = new FormData()
      form.append('file', file)
      form.append('name', file.name)
      return api.upload<FloorPlan>(`/floors/${floorId}/floorplan`, form)
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.floorPlan(floorId) })
      void queryClient.invalidateQueries({ queryKey: keys.floorPlans(floorId) })
      invalidateProject(queryClient, projectId)
    },
  })
}

export function useCreateCaptureSession() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: Record<string, unknown>) => api.post<CaptureSession>('/captures', body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['captures'] }),
  })
}

export function useUploadCaptureFiles(sessionId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (files: File[]) => {
      const form = new FormData()
      for (const file of files) form.append('files', file)
      return api.upload<CaptureFileDetail[]>(`/captures/${sessionId}/upload`, form)
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.capture(sessionId) })
      void queryClient.invalidateQueries({ queryKey: keys.captureFiles(sessionId) })
    },
  })
}

export function useProcessCapture(sessionId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body?: { frame_interval_seconds?: number; force?: boolean }) =>
      api.post<ProcessingStatus>(`/captures/${sessionId}/process`, body ?? {}),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.captureStatus(sessionId) })
      void queryClient.invalidateQueries({ queryKey: keys.capture(sessionId) })
    },
  })
}

export function useRetryCapture(sessionId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<ProcessingStatus>(`/captures/${sessionId}/retry`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.captureStatus(sessionId) })
      void queryClient.invalidateQueries({ queryKey: keys.capture(sessionId) })
    },
  })
}

export function useCreateLocation(floorId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: { x: number; y: number; code?: string; label?: string; zone_id?: string }) =>
      api.post<CaptureLocation>(`/floors/${floorId}/locations`, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.locations(floorId) }),
  })
}

export function useMoveLocation(floorId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; x?: number; y?: number; label?: string; zone_id?: string }) =>
      api.patch<CaptureLocation>(`/locations/${id}`, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.locations(floorId) })
      void queryClient.invalidateQueries({ queryKey: keys.floorPaths(floorId) })
    },
  })
}

export function useDeleteLocation(floorId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.delete<Message>(`/locations/${id}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.locations(floorId) })
      void queryClient.invalidateQueries({ queryKey: keys.floorPaths(floorId) })
    },
  })
}

export function useAssignFrameLocation(floorId?: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      frameId,
      ...body
    }: {
      frameId: string
      location_id?: string
      x?: number
      y?: number
      code?: string
    }) => api.put<CaptureFrameDetail>(`/frames/${frameId}/location`, body),
    onSuccess: (frame) => {
      void queryClient.invalidateQueries({ queryKey: keys.viewer(frame.id) })
      void queryClient.invalidateQueries({ queryKey: ['capture', frame.session_id] })
      if (floorId) {
        void queryClient.invalidateQueries({ queryKey: keys.locations(floorId) })
        void queryClient.invalidateQueries({ queryKey: keys.floorPaths(floorId) })
      }
    },
  })
}

export function useCreateAnnotation(frameId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.post<Annotation>(`/frames/${frameId}/annotations`, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.frameAnnotations(frameId) })
      void queryClient.invalidateQueries({ queryKey: keys.viewer(frameId) })
      void queryClient.invalidateQueries({ queryKey: ['project'] })
    },
  })
}

export function useUpdateAnnotation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string } & Record<string, unknown>) =>
      api.patch<Annotation>(`/annotations/${id}`, body),
    onSuccess: (annotation) => {
      void queryClient.invalidateQueries({ queryKey: keys.frameAnnotations(annotation.frame_id) })
      void queryClient.invalidateQueries({ queryKey: ['project', annotation.project_id] })
    },
  })
}

export function useGenerateReport(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: {
      report_type: string
      report_format: string
      floor_id?: string
      period_start?: string
      period_end?: string
    }) => api.post<Report>(`/projects/${projectId}/reports`, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.reports(projectId) }),
  })
}

export function useUpsertSchedule(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: {
      floor_id?: string | null
      expected_per_week: number
      grace_days: number
      notes?: string
    }) => api.put<CaptureSchedule>(`/projects/${projectId}/schedule`, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.schedules(projectId) })
      invalidateProject(queryClient, projectId)
    },
  })
}

function invalidateProject(queryClient: ReturnType<typeof useQueryClient>, projectId: string) {
  void queryClient.invalidateQueries({ queryKey: ['project', projectId] })
  void queryClient.invalidateQueries({ queryKey: ['projects'] })
  void queryClient.invalidateQueries({ queryKey: keys.portfolio })
}
