"""Canonical enumerations.

These values are the contract between backend, frontend and mobile. They are mirrored into
``shared/contracts.json`` (and from there into TypeScript) by
``infrastructure/scripts/export_contracts.py``, so a status string can never drift between the
three codebases.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    PROJECT_MANAGER = "project_manager"
    SITE_ENGINEER = "site_engineer"
    VIEWER = "viewer"


#: Strict privilege ordering. ``ROLE_RANK[a] >= ROLE_RANK[b]`` means "a can do everything b can".
ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.SITE_ENGINEER: 1,
    Role.PROJECT_MANAGER: 2,
    Role.ADMIN: 3,
}


class ProjectStatus(StrEnum):
    PLANNING = "planning"
    ACTIVE = "active"
    ON_HOLD = "on_hold"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class CaptureSessionStatus(StrEnum):
    """Lifecycle of a capture session (§34)."""

    DRAFT = "draft"
    RECORDING = "recording"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    EXTRACTING_FRAMES = "extracting_frames"
    ANALYZING = "analyzing"
    MAPPING = "mapping"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Statuses that mean "no more work will happen without user action".
TERMINAL_SESSION_STATUSES = frozenset(
    {CaptureSessionStatus.READY, CaptureSessionStatus.FAILED, CaptureSessionStatus.CANCELLED}
)


class CaptureSource(StrEnum):
    """Who or what produced the capture (§47) — robots/drones need no schema change."""

    HUMAN_OPERATOR = "human_operator"
    ROBOT = "robot"
    DRONE = "drone"
    FIXED_CAMERA = "fixed_camera"


class FileKind(StrEnum):
    PANORAMA = "panorama"
    PERSPECTIVE_IMAGE = "perspective_image"
    VIDEO_360 = "video_360"
    VIDEO_FLAT = "video_flat"
    UNKNOWN = "unknown"


class ProjectionType(StrEnum):
    EQUIRECTANGULAR = "equirectangular"
    PERSPECTIVE = "perspective"
    UNKNOWN = "unknown"


class JobType(StrEnum):
    VALIDATE = "validate"
    METADATA = "metadata"
    FRAME_EXTRACTION = "frame_extraction"
    QUALITY = "quality"
    FEATURES = "features"
    MAPPING = "mapping"
    VIEWER_DATA = "viewer_data"
    THUMBNAILS = "thumbnails"


#: The pipeline order (§8). Stages run in this sequence; each is independent and retryable.
PIPELINE_ORDER: tuple[JobType, ...] = (
    JobType.VALIDATE,
    JobType.METADATA,
    JobType.FRAME_EXTRACTION,
    JobType.QUALITY,
    JobType.FEATURES,
    JobType.MAPPING,
    JobType.VIEWER_DATA,
)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


class QualityVerdict(StrEnum):
    GOOD = "good"
    WARNING = "warning"
    POOR = "poor"
    UNUSABLE = "unusable"


class DataProvenance(StrEnum):
    """§46 — the platform must never present an estimate as a measurement."""

    MEASURED = "measured"
    ESTIMATED = "estimated"
    AI_PREDICTED = "ai_predicted"
    MANUAL = "manual"


class MappingConfidence(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AnnotationPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AnnotationStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


class CaptureFrequencyStatus(StrEnum):
    OK = "ok"
    DUE = "due"
    OVERDUE = "overdue"
    NEVER_CAPTURED = "never_captured"


class ReportType(StrEnum):
    PROJECT_SUMMARY = "project_summary"
    FLOOR_COVERAGE = "floor_coverage"
    CAPTURE_HISTORY = "capture_history"
    ANNOTATION_LOG = "annotation_log"


class ReportFormat(StrEnum):
    PDF = "pdf"
    CSV = "csv"
    JSON = "json"


class ReportStatus(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"


class CameraTransport(StrEnum):
    FILE_UPLOAD = "file_upload"
    FOLDER_IMPORT = "folder_import"
    WIFI_OSC = "wifi_osc"
    USB = "usb"
    BLUETOOTH = "bluetooth"
