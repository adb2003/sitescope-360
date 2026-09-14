/**
 * Types mirroring the backend's Pydantic schemas.
 *
 * The string-union types below are generated into `shared/contracts.ts` from the backend's
 * `StrEnum` definitions by `infrastructure/scripts/export_contracts.py`, and re-exported here.
 * Hand-writing them would let a status value drift silently between the two codebases.
 */

export type {
  AnnotationPriority,
  AnnotationStatus,
  CameraTransport,
  CaptureFrequencyStatus,
  CaptureSessionStatus,
  CaptureSource,
  DataProvenance,
  FileKind,
  JobStatus,
  JobType,
  MappingConfidence,
  ProjectStatus,
  ProjectionType,
  QualityVerdict,
  ReportFormat,
  ReportStatus,
  ReportType,
  Role,
} from '../../../shared/contracts'

import type {
  AnnotationPriority,
  AnnotationStatus,
  CameraTransport,
  CaptureFrequencyStatus,
  CaptureSessionStatus,
  CaptureSource,
  DataProvenance,
  FileKind,
  JobStatus,
  JobType,
  MappingConfidence,
  ProjectStatus,
  ProjectionType,
  QualityVerdict,
  ReportFormat,
  ReportStatus,
  ReportType,
  Role,
} from '../../../shared/contracts'

// ---- envelopes --------------------------------------------------------------
export interface ErrorEnvelope {
  code: string
  message: string
  details?: Record<string, unknown>
}

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface Message {
  message: string
}

export interface SignedAsset {
  key: string
  url: string
  expires_in: number
}

// ---- auth -------------------------------------------------------------------
export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  expires_at: string
}

export interface User {
  id: string
  email: string
  full_name: string
  role: Role
  is_active: boolean
  last_login_at: string | null
  created_at: string
}

export interface SessionInfo {
  user: User
  capabilities: string[]
}

// ---- project hierarchy ------------------------------------------------------
export interface Project {
  id: string
  name: string
  code: string
  client: string | null
  location: string | null
  description: string | null
  status: ProjectStatus
  start_date: string | null
  expected_completion_date: string | null
  project_manager_id: string | null
  expected_captures_per_week: number
  created_at: string
  updated_at: string
}

export interface ProjectSummary extends Project {
  building_count: number
  floor_count: number
  zone_count: number
  capture_session_count: number
  frame_count: number
  location_count: number
  last_capture_at: string | null
}

export interface Building {
  id: string
  project_id: string
  name: string
  description: string | null
  created_at: string
}

export interface Floor {
  id: string
  building_id: string
  name: string
  level: number
  area_sqft: number | null
  elevation_m: number | null
  created_at: string
}

export interface FloorDetail extends Floor {
  project_id: string
  building_name: string
  zone_count: number
  location_count: number
  capture_session_count: number
  active_floor_plan_id: string | null
  last_capture_at: string | null
}

export interface Zone {
  id: string
  floor_id: string
  name: string
  description: string | null
  area_sqft: number | null
  expected_capture_points: number
  created_at: string
}

export interface FloorPlan {
  id: string
  floor_id: string
  name: string
  content_type: string
  width_px: number
  height_px: number
  file_size_bytes: number
  px_per_meter: number | null
  calibration: Record<string, unknown> | null
  is_active: boolean
  created_at: string
  asset: SignedAsset | null
}

export interface CaptureLocation {
  id: string
  floor_id: string
  zone_id: string | null
  floor_plan_id: string | null
  code: string
  label: string | null
  x: number
  y: number
  provenance: DataProvenance
  confidence: number | null
  created_at: string
  frame_count: number
  latest_frame_id: string | null
  latest_captured_at: string | null
}

export interface CaptureSchedule {
  id: string
  project_id: string
  floor_id: string | null
  expected_per_week: number
  grace_days: number
  notes: string | null
}

// ---- captures ---------------------------------------------------------------

/**
 * What loop closure did to a track.
 *
 * Mirrors `SlamOutcome.to_dict()` on the backend. Present so the UI can say which estimator
 * produced the positions it is drawing -- the coordinates look the same either way, and a
 * loop-closed track has bounded drift where a dead-reckoned one does not.
 */
export interface LoopClosureReport {
  method: string
  loop_closures: Array<{
    from_index: number
    to_index: number
    dtheta_deg: number
    inlier_count: number
    hash_distance: number
    confidence: number
  }>
  optimisation: {
    converged: boolean
    iterations: number
    initial_error: number
    final_error: number
    error_reduction: number
    max_correction: number
    /** The same correction as a multiple of the track's median step -- the readable form. */
    max_correction_steps: number
    loop_closures_used: number
    loop_closures_rejected: number
    notes: string[]
  }
  confidence: string
}

export interface CaptureSession {
  id: string
  project_id: string
  building_id: string | null
  floor_id: string
  zone_id: string | null
  name: string | null
  status: CaptureSessionStatus
  failure_reason: string | null
  started_at: string
  ended_at: string | null
  operator_id: string | null
  capture_source: CaptureSource
  camera_device_id: string | null
  camera_label: string | null
  frame_interval_seconds: number | null
  notes: string | null
  gps_latitude: number | null
  gps_longitude: number | null
  mapping_confidence: MappingConfidence
  mapping_confidence_score: number | null
  mapping_note: string | null
  /** "dead_reckoning" | "pose_graph_slam"; null for sessions mapped before this was recorded. */
  mapping_method: string | null
  /** Null when loop closure found nothing to reconcile. */
  loop_closure_report: LoopClosureReport | null
  frame_count: number
  file_count: number
  created_at: string
}

export interface CaptureSessionDetail extends CaptureSession {
  project_name: string | null
  floor_name: string | null
  building_name: string | null
  zone_name: string | null
  operator_name: string | null
  files: CaptureFile[]
  jobs: ProcessingJob[]
  quality_summary: Record<string, number>
}

export interface CaptureFile {
  id: string
  session_id: string
  original_filename: string
  content_type: string
  file_size_bytes: number
  checksum_sha256: string | null
  kind: FileKind
  projection: ProjectionType
  width_px: number | null
  height_px: number | null
  duration_seconds: number | null
  fps: number | null
  captured_at: string | null
  camera_make: string | null
  camera_model: string | null
  gps_latitude: number | null
  gps_longitude: number | null
  gps_altitude_m: number | null
  validation_errors: string[] | null
  validation_warnings: string[] | null
  created_at: string
}

export interface CaptureFileDetail extends CaptureFile {
  metadata_json: Record<string, unknown> | null
  frame_count: number
  asset: SignedAsset | null
}

/** Where to PUT one file, and what the server insists on receiving with it. */
export interface DirectUploadTicket {
  storage_key: string
  upload_url: string
  method: string
  headers: Record<string, string>
  expires_in: number
}

/** A capture point near a plan coordinate, with how far away it actually was. */
export interface NearestCaptureLocation {
  location: CaptureLocation
  distance_px: number
}

export interface Quality {
  quality_score: number
  blur_score: number
  brightness_score: number
  contrast_score: number
  resolution_score: number
  completeness_score: number
  verdict: QualityVerdict
  is_usable: boolean
  is_duplicate: boolean
  duplicate_of_frame_id: string | null
  warnings: string[]
  raw_metrics: Record<string, unknown> | null
}

export interface CaptureFrame {
  id: string
  session_id: string
  file_id: string
  sequence_index: number
  source_time_seconds: number
  width_px: number
  height_px: number
  projection: ProjectionType
  captured_at: string
  location_id: string | null
  rel_x: number | null
  rel_y: number | null
  heading_deg: number | null
  position_provenance: DataProvenance | null
  position_confidence: number | null
  initial_yaw_deg: number
  is_keyframe: boolean
  feature_count: number | null
  perceptual_hash: string | null
  quality: Quality | null
}

export interface CaptureFrameDetail extends CaptureFrame {
  location_code: string | null
  floor_id: string | null
  floor_name: string | null
  project_id: string | null
  project_name: string | null
  zone_name: string | null
  thumbnail: SignedAsset | null
  preview: SignedAsset | null
  full: SignedAsset | null
  annotation_count: number
}

export interface FrameHistoryEntry {
  frame_id: string
  session_id: string
  captured_at: string
  quality_verdict: QualityVerdict | null
  thumbnail_url: string | null
}

export interface ViewerNeighbour {
  frame_id: string
  location_code: string | null
  direction_deg: number | null
  distance: number | null
}

export interface ViewerManifest {
  frame: CaptureFrameDetail
  neighbours: ViewerNeighbour[]
  previous_frame_id: string | null
  next_frame_id: string | null
  history: FrameHistoryEntry[]
}

export interface ProcessingJob {
  id: string
  session_id: string
  file_id: string | null
  job_type: JobType
  status: JobStatus
  attempt: number
  max_attempts: number
  progress: number
  message: string | null
  error_code: string | null
  error_detail: string | null
  started_at: string | null
  finished_at: string | null
  duration_seconds: number | null
  result: Record<string, unknown> | null
  created_at: string
}

export interface ProcessingStatus {
  session_id: string
  status: CaptureSessionStatus
  failure_reason: string | null
  progress: number
  stages: ProcessingJob[]
}

export interface CapturePathStep {
  location_id: string | null
  frame_id: string
  code: string | null
  x: number | null
  y: number | null
  timestamp: string
}

export interface CapturePath {
  id: string
  session_id: string
  floor_id: string
  steps: CapturePathStep[]
  provenance: DataProvenance
  confidence: number | null
}

export interface FrameComparison {
  a: CaptureFrameDetail
  b: CaptureFrameDetail
  same_location: boolean
  days_apart: number
  note: string | null
}

// ---- monitoring -------------------------------------------------------------
export interface ZoneCoverage {
  zone_id: string
  zone_name: string
  area_sqft: number | null
  expected_capture_points: number
  actual_capture_points: number
  captured_points_recent: number
  coverage_ratio: number
  covered_area_sqft: number | null
  pending_area_sqft: number | null
}

export interface FloorCoverage {
  floor_id: string
  floor_name: string
  level: number
  area_sqft: number | null
  expected_capture_points: number
  actual_capture_points: number
  visited_locations: number
  coverage_ratio: number
  covered_area_sqft: number | null
  pending_area_sqft: number | null
  last_capture_at: string | null
  zones: ZoneCoverage[]
}

export interface Coverage {
  project_id: string
  coverage_ratio: number
  total_area_sqft: number | null
  covered_area_sqft: number | null
  pending_area_sqft: number | null
  provenance: DataProvenance
  method: string
  floors: FloorCoverage[]
}

export interface TimelineEntry {
  capture_date: string
  session_ids: string[]
  session_count: number
  frame_count: number
  located_frame_count: number
  coverage_ratio: number
  coverage_provenance: DataProvenance
  floors: string[]
  operators: string[]
}

export interface Timeline {
  project_id: string
  floor_id: string | null
  entries: TimelineEntry[]
  location_total: number
  note: string | null
}

export interface FrequencyStatus {
  project_id: string
  project_name: string
  floor_id: string | null
  floor_name: string | null
  expected_per_week: number
  last_capture_at: string | null
  days_since_last_capture: number | null
  next_expected_date: string | null
  status: CaptureFrequencyStatus
  captures_this_week: number
}

export interface CaptureHealth {
  total_sessions: number
  completed: number
  in_progress: number
  pending: number
  failed: number
}

export interface FloorStatus {
  floor_id: string
  floor_name: string
  level: number
  coverage_ratio: number
  location_count: number
  visited_locations: number
  last_capture_at: string | null
}

export interface RecentCapture {
  session_id: string
  captured_at: string
  operator_name: string | null
  floor_name: string
  zone_name: string | null
  frame_count: number
  quality_verdict: QualityVerdict | null
  status: CaptureSessionStatus
  mapping_confidence: MappingConfidence
}

export interface QualityBreakdown {
  good: number
  warning: number
  poor: number
  unusable: number
  unscored: number
}

export interface ProjectDashboard {
  project_id: string
  project_name: string
  project_code: string
  building_count: number
  floor_count: number
  zone_count: number
  location_count: number
  session_count: number
  frame_count: number
  annotation_open_count: number
  last_capture_at: string | null
  coverage: Coverage
  capture_health: CaptureHealth
  quality: QualityBreakdown
  frequency: FrequencyStatus
  floors: FloorStatus[]
  recent_captures: RecentCapture[]
  is_empty: boolean
}

export interface PortfolioRow {
  project_id: string
  project_name: string
  project_code: string
  status: string
  floor_count: number
  session_count: number
  coverage_ratio: number
  last_capture_at: string | null
  frequency_status: CaptureFrequencyStatus
  open_annotations: number
}

export interface Portfolio {
  projects: PortfolioRow[]
  total_projects: number
  total_sessions: number
  total_frames: number
  overdue_count: number
}

export interface StaleLocation {
  location_id: string
  code: string
  label: string | null
  floor_id: string
  floor_name: string
  zone_name: string | null
  last_captured_at: string | null
  days_since_capture: number | null
}

export interface StaleAreas {
  project_id: string
  threshold_days: number
  never_captured: StaleLocation[]
  stale: StaleLocation[]
}

// ---- annotations ------------------------------------------------------------
export interface Annotation {
  id: string
  frame_id: string
  project_id: string
  floor_id: string
  yaw_deg: number
  pitch_deg: number
  title: string
  description: string | null
  priority: AnnotationPriority
  status: AnnotationStatus
  created_by_id: string | null
  assigned_to_id: string | null
  resolved_at: string | null
  created_at: string
  updated_at: string
  created_by_name?: string | null
  assigned_to_name?: string | null
  floor_name?: string | null
  location_code?: string | null
  captured_at?: string | null
}

// ---- reports ----------------------------------------------------------------
export interface Report {
  id: string
  project_id: string
  floor_id: string | null
  report_type: ReportType
  report_format: ReportFormat
  status: ReportStatus
  title: string
  period_start: string | null
  period_end: string | null
  file_size_bytes: number | null
  error_detail: string | null
  generated_at: string | null
  created_at: string
  asset: SignedAsset | null
  payload?: Record<string, unknown> | null
}

export interface ReportTypeInfo {
  key: ReportType
  name: string
  description: string
}

export interface ReportCatalogue {
  types: ReportTypeInfo[]
  formats: ReportFormat[]
}

// ---- cameras ----------------------------------------------------------------
export interface CameraProviderInfo {
  key: string
  display_name: string
  transport: CameraTransport
  implemented: boolean
  description: string
  supports_live_transfer: boolean
  supports_remote_trigger: boolean
  accepted_extensions: string[]
  setup_hint: string | null
}

export interface CameraDevice {
  id: string
  label: string
  make: string | null
  model: string | null
  serial_number: string | null
  provider_key: string
  transport: CameraTransport
  capture_source: CaptureSource
  default_projection: ProjectionType
  native_width_px: number | null
  native_height_px: number | null
  is_active: boolean
  last_seen_at: string | null
  config: Record<string, unknown> | null
  notes: string | null
  created_at: string
}

// ---- system -----------------------------------------------------------------
export interface CapabilityEntry {
  implemented: boolean
  method?: string
  provenance?: DataProvenance
  limitations?: string[]
  phase?: number
  note?: string
}

export interface SystemCapabilities {
  vision: Record<string, CapabilityEntry>
  bim: {
    implemented: boolean
    phase: number
    interfaces_defined: string[]
    schema_ready: string[]
    still_required: string[]
    note: string
  }
  provenance_levels: DataProvenance[]
  mapping_confidence_threshold: number
  processing: {
    queue_backend: string
    default_frame_interval_seconds: number
    max_frames_per_video: number
  }
  uploads: {
    max_bytes: number
    image_extensions: string[]
    video_extensions: string[]
    floorplan_extensions: string[]
  }
  storage_backend: string
  independence: {
    third_party_monitoring_platforms: string[]
    statement: string
  }
}

export interface JobSummary {
  by_status: Record<string, number>
  by_stage: Record<string, number>
  mean_duration_seconds: Record<string, number>
  recent_failures: Array<{
    job_id: string
    session_id: string
    stage: string
    error_code: string | null
    message: string | null
    finished_at: string | null
  }>
  queue: Record<string, unknown>
}
