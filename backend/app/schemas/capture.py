from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from app.core.enums import (
    CaptureSessionStatus,
    CaptureSource,
    DataProvenance,
    FileKind,
    JobStatus,
    JobType,
    MappingConfidence,
    ProjectionType,
    QualityVerdict,
)
from app.schemas.common import ORMModel, SignedAsset


# ---- session -----------------------------------------------------------------
class CaptureSessionCreate(BaseModel):
    floor_id: str
    zone_id: str | None = None
    name: str | None = Field(default=None, max_length=200)
    started_at: datetime | None = None
    capture_source: CaptureSource = CaptureSource.HUMAN_OPERATOR
    camera_device_id: str | None = None
    camera_label: str | None = Field(default=None, max_length=200)
    frame_interval_seconds: Annotated[float, Field(gt=0, le=60)] | None = None
    notes: str | None = None
    gps_latitude: Annotated[float, Field(ge=-90, le=90)] | None = None
    gps_longitude: Annotated[float, Field(ge=-180, le=180)] | None = None
    #: Lets an offline mobile client keep the id it generated locally, so a retried
    #: "create session" after a network failure is idempotent rather than duplicating.
    client_session_id: str | None = Field(default=None, max_length=40)


class CaptureSessionUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    zone_id: str | None = None
    ended_at: datetime | None = None
    notes: str | None = None
    frame_interval_seconds: Annotated[float, Field(gt=0, le=60)] | None = None


class CaptureSessionOut(ORMModel):
    id: str
    project_id: str
    building_id: str | None
    floor_id: str
    zone_id: str | None
    name: str | None
    status: CaptureSessionStatus
    failure_reason: str | None
    started_at: datetime
    ended_at: datetime | None
    operator_id: str | None
    capture_source: CaptureSource
    camera_device_id: str | None
    camera_label: str | None
    frame_interval_seconds: float | None
    notes: str | None
    gps_latitude: float | None
    gps_longitude: float | None
    mapping_confidence: MappingConfidence
    mapping_confidence_score: float | None
    mapping_note: str | None
    #: "dead_reckoning" or "pose_graph_slam"; null for sessions mapped before this was recorded.
    mapping_method: str | None = None
    #: Verified revisits, rejected false positives, and how far the optimiser moved the track.
    #: Null when loop closure found nothing to reconcile.
    loop_closure_report: dict[str, Any] | None = None
    frame_count: int
    file_count: int
    created_at: datetime


class CaptureSessionDetail(CaptureSessionOut):
    project_name: str | None = None
    floor_name: str | None = None
    building_name: str | None = None
    zone_name: str | None = None
    operator_name: str | None = None
    files: list[CaptureFileOut] = []
    jobs: list[ProcessingJobOut] = []
    quality_summary: dict[str, int] = {}


# ---- file --------------------------------------------------------------------
class DirectUploadRequest(BaseModel):
    """Ask for somewhere to put one file."""

    filename: Annotated[str, Field(min_length=1, max_length=400)]
    content_type: str | None = None


class DirectUploadTicketOut(BaseModel):
    """Where to PUT the bytes, and what to send with them.

    ``headers`` is not advisory. S3 signs the content type into the presigned URL, so a client
    that drops the header gets a 403 rather than an object with a guessed type — returning the
    headers alongside the URL is what saves every client from having to know that.
    """

    storage_key: str
    upload_url: str
    method: str = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)
    expires_in: int


class MultipartUploadRequest(BaseModel):
    """Ask to upload one file in pieces.

    ``size_bytes`` is mandatory, unlike on the single-PUT route. The part boundaries are derived
    from it and both ends must derive the same ones — a client that sends the byte ranges of a
    different plan than the server assembles produces a corrupt object out of two sets of
    perfectly valid parts.
    """

    filename: Annotated[str, Field(min_length=1, max_length=400)]
    size_bytes: Annotated[int, Field(gt=0)]
    content_type: str | None = None


class MultipartResumeRequest(BaseModel):
    """Come back to an upload already in flight, after a crash or an expired link."""

    storage_key: str
    upload_id: Annotated[str, Field(min_length=1, max_length=400)]
    filename: Annotated[str, Field(min_length=1, max_length=400)]
    size_bytes: Annotated[int, Field(gt=0)]


class MultipartCompleteRequest(MultipartResumeRequest):
    """Every part has been sent; assemble and adopt the object."""


class UploadPartOut(BaseModel):
    """Where one part goes, and how big it must be.

    ``size_bytes`` is not advice. It is the length of the byte range the server will treat as
    this part, so a client that sends a different amount has silently changed where every later
    part begins.
    """

    part_number: int
    upload_url: str
    method: str = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)
    size_bytes: int


class MultipartTicketOut(BaseModel):
    """The plan for a chunked upload, and what is left of it.

    ``received`` and ``parts`` are complements: the part numbers storage is already holding, and
    a URL for each one that still has to be sent. On a fresh upload the first is empty; on a
    resume it is the answer to the only question a recovering client cannot answer for itself.
    """

    storage_key: str
    upload_id: str
    part_size: int
    part_count: int
    total_bytes: int
    received: list[int] = Field(default_factory=list)
    parts: list[UploadPartOut] = Field(default_factory=list)
    expires_in: int


class DirectUploadRegistration(BaseModel):
    """Tell the server the bytes have landed, so it can check them and take ownership."""

    storage_key: str
    filename: Annotated[str, Field(min_length=1, max_length=400)]


class TrackAnchor(BaseModel):
    """One frame the operator has identified on the floor plan."""

    frame_id: str
    x: float
    y: float


class AlignTrackRequest(BaseModel):
    """Two or more anchors. Fewer leaves rotation and scale undetermined, not merely imprecise."""

    anchors: Annotated[list[TrackAnchor], Field(min_length=2, max_length=50)]


class AlignTrackResult(BaseModel):
    """What the fit did, in terms the operator can check.

    ``rms_error_px`` is the number that matters: it is how far the anchors ended up from where
    they were dropped, and a large one means the estimated track does not describe the same walk
    the anchors do — so the positions should not be trusted even though they exist.
    """

    placed_frames: int
    scale: float
    rotation_deg: float
    rms_error_px: float
    note: str


class CaptureFileOut(ORMModel):
    id: str
    session_id: str
    original_filename: str
    content_type: str
    file_size_bytes: int
    checksum_sha256: str | None
    kind: FileKind
    projection: ProjectionType
    width_px: int | None
    height_px: int | None
    duration_seconds: float | None
    fps: float | None
    captured_at: datetime | None
    camera_make: str | None
    camera_model: str | None
    gps_latitude: float | None
    gps_longitude: float | None
    gps_altitude_m: float | None
    validation_errors: list[str] | None
    validation_warnings: list[str] | None
    created_at: datetime


class CaptureFileDetail(CaptureFileOut):
    metadata_json: dict[str, Any] | None = None
    frame_count: int = 0
    asset: SignedAsset | None = None


# ---- quality -----------------------------------------------------------------
class QualityOut(ORMModel):
    quality_score: float
    blur_score: float
    brightness_score: float
    contrast_score: float
    resolution_score: float
    completeness_score: float
    verdict: QualityVerdict
    is_usable: bool
    is_duplicate: bool
    duplicate_of_frame_id: str | None
    warnings: list[str]
    raw_metrics: dict[str, Any] | None = None


# ---- frame -------------------------------------------------------------------
class CaptureFrameOut(ORMModel):
    id: str
    session_id: str
    file_id: str
    sequence_index: int
    source_time_seconds: float
    width_px: int
    height_px: int
    projection: ProjectionType
    captured_at: datetime
    location_id: str | None
    rel_x: float | None
    rel_y: float | None
    heading_deg: float | None
    position_provenance: DataProvenance | None
    position_confidence: float | None
    initial_yaw_deg: float
    is_keyframe: bool
    feature_count: int | None
    perceptual_hash: str | None
    quality: QualityOut | None = None


class CaptureFrameDetail(CaptureFrameOut):
    location_code: str | None = None
    floor_id: str | None = None
    floor_name: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    zone_name: str | None = None
    thumbnail: SignedAsset | None = None
    preview: SignedAsset | None = None
    full: SignedAsset | None = None
    annotation_count: int = 0


class FrameAssignLocation(BaseModel):
    location_id: str | None = None
    #: Alternatively create-and-assign in one call by giving floor-plan coordinates.
    x: float | None = None
    y: float | None = None
    code: str | None = None


class ViewerNeighbour(BaseModel):
    frame_id: str
    location_code: str | None
    direction_deg: float | None
    distance: float | None


class ViewerManifest(BaseModel):
    """Everything the 360 viewer needs for one frame, in one round trip (§11)."""

    frame: CaptureFrameDetail
    neighbours: list[ViewerNeighbour] = []
    previous_frame_id: str | None = None
    next_frame_id: str | None = None
    #: Same location, other dates — powers the compare view and the history strip.
    history: list[FrameHistoryEntry] = []


class FrameHistoryEntry(BaseModel):
    frame_id: str
    session_id: str
    captured_at: datetime
    quality_verdict: QualityVerdict | None = None
    thumbnail_url: str | None = None


# ---- jobs --------------------------------------------------------------------
class ProcessingJobOut(ORMModel):
    id: str
    session_id: str
    file_id: str | None
    job_type: JobType
    status: JobStatus
    attempt: int
    max_attempts: int
    progress: float
    message: str | None
    error_code: str | None
    error_detail: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    result: dict[str, Any] | None
    created_at: datetime


class ProcessRequest(BaseModel):
    frame_interval_seconds: Annotated[float, Field(gt=0, le=60)] | None = None
    #: Re-run stages that already completed (used by the retry button).
    force: bool = False
    stages: list[JobType] | None = None


class ProcessingStatus(BaseModel):
    session_id: str
    status: CaptureSessionStatus
    failure_reason: str | None = None
    progress: float
    stages: list[ProcessingJobOut]


# ---- path --------------------------------------------------------------------
class CapturePathStep(BaseModel):
    location_id: str | None
    frame_id: str
    code: str | None
    x: float | None
    y: float | None
    timestamp: datetime


class CapturePathOut(ORMModel):
    id: str
    session_id: str
    floor_id: str
    steps: list[dict[str, Any]]
    provenance: DataProvenance
    confidence: float | None


class MappingResultOut(BaseModel):
    """Honest reporting of automatic mapping (§14, §46)."""

    session_id: str
    confidence: MappingConfidence
    confidence_score: float | None
    note: str
    requires_manual_placement: bool
    estimated_positions: list[dict[str, Any]] = []


CaptureSessionDetail.model_rebuild()
ViewerManifest.model_rebuild()
