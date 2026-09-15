// GENERATED FILE — do not edit.
// Source: backend/app/core/enums.py
// Regenerate: python infrastructure/scripts/export_contracts.py

export type Role = 'admin' | 'project_manager' | 'site_engineer' | 'viewer'

export const ROLE_VALUES: readonly Role[] = ['admin', 'project_manager', 'site_engineer', 'viewer'] as const

export type ProjectStatus = 'planning' | 'active' | 'on_hold' | 'completed' | 'archived'

export const PROJECT_STATUS_VALUES: readonly ProjectStatus[] = ['planning', 'active', 'on_hold', 'completed', 'archived'] as const

export type CaptureSessionStatus = 'draft' | 'recording' | 'uploading' | 'uploaded' | 'processing' | 'extracting_frames' | 'analyzing' | 'mapping' | 'ready' | 'failed' | 'cancelled'

export const CAPTURE_SESSION_STATUS_VALUES: readonly CaptureSessionStatus[] = ['draft', 'recording', 'uploading', 'uploaded', 'processing', 'extracting_frames', 'analyzing', 'mapping', 'ready', 'failed', 'cancelled'] as const

export type CaptureSource = 'human_operator' | 'robot' | 'drone' | 'fixed_camera'

export const CAPTURE_SOURCE_VALUES: readonly CaptureSource[] = ['human_operator', 'robot', 'drone', 'fixed_camera'] as const

export type FileKind = 'panorama' | 'perspective_image' | 'video_360' | 'video_flat' | 'unknown'

export const FILE_KIND_VALUES: readonly FileKind[] = ['panorama', 'perspective_image', 'video_360', 'video_flat', 'unknown'] as const

export type ProjectionType = 'equirectangular' | 'perspective' | 'dual_fisheye' | 'unknown'

export const PROJECTION_TYPE_VALUES: readonly ProjectionType[] = ['equirectangular', 'perspective', 'dual_fisheye', 'unknown'] as const

export type JobType = 'validate' | 'metadata' | 'frame_extraction' | 'quality' | 'features' | 'mapping' | 'viewer_data' | 'thumbnails'

export const JOB_TYPE_VALUES: readonly JobType[] = ['validate', 'metadata', 'frame_extraction', 'quality', 'features', 'mapping', 'viewer_data', 'thumbnails'] as const

export type JobStatus = 'queued' | 'running' | 'complete' | 'failed' | 'skipped'

export const JOB_STATUS_VALUES: readonly JobStatus[] = ['queued', 'running', 'complete', 'failed', 'skipped'] as const

export type QualityVerdict = 'good' | 'warning' | 'poor' | 'unusable'

export const QUALITY_VERDICT_VALUES: readonly QualityVerdict[] = ['good', 'warning', 'poor', 'unusable'] as const

export type DataProvenance = 'measured' | 'estimated' | 'ai_predicted' | 'manual'

export const DATA_PROVENANCE_VALUES: readonly DataProvenance[] = ['measured', 'estimated', 'ai_predicted', 'manual'] as const

export type MappingConfidence = 'none' | 'low' | 'medium' | 'high'

export const MAPPING_CONFIDENCE_VALUES: readonly MappingConfidence[] = ['none', 'low', 'medium', 'high'] as const

export type AnnotationPriority = 'low' | 'medium' | 'high' | 'critical'

export const ANNOTATION_PRIORITY_VALUES: readonly AnnotationPriority[] = ['low', 'medium', 'high', 'critical'] as const

export type AnnotationStatus = 'open' | 'in_progress' | 'resolved'

export const ANNOTATION_STATUS_VALUES: readonly AnnotationStatus[] = ['open', 'in_progress', 'resolved'] as const

export type CaptureFrequencyStatus = 'ok' | 'due' | 'overdue' | 'never_captured'

export const CAPTURE_FREQUENCY_STATUS_VALUES: readonly CaptureFrequencyStatus[] = ['ok', 'due', 'overdue', 'never_captured'] as const

export type ReportType = 'project_summary' | 'floor_coverage' | 'capture_history' | 'annotation_log'

export const REPORT_TYPE_VALUES: readonly ReportType[] = ['project_summary', 'floor_coverage', 'capture_history', 'annotation_log'] as const

export type ReportFormat = 'pdf' | 'csv' | 'json'

export const REPORT_FORMAT_VALUES: readonly ReportFormat[] = ['pdf', 'csv', 'json'] as const

export type ReportStatus = 'queued' | 'generating' | 'ready' | 'failed'

export const REPORT_STATUS_VALUES: readonly ReportStatus[] = ['queued', 'generating', 'ready', 'failed'] as const

export type CameraTransport = 'file_upload' | 'folder_import' | 'wifi_osc' | 'usb' | 'bluetooth'

export const CAMERA_TRANSPORT_VALUES: readonly CameraTransport[] = ['file_upload', 'folder_import', 'wifi_osc', 'usb', 'bluetooth'] as const

export const PIPELINE_ORDER: readonly JobType[] = ['validate', 'metadata', 'frame_extraction', 'quality', 'features', 'mapping', 'viewer_data'] as const

export const ROLE_RANK: Readonly<Record<Role, number>> = {viewer: 0, site_engineer: 1, project_manager: 2, admin: 3} as const
