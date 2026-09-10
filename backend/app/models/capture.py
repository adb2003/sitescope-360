"""Capture session, files, frames, locations, paths, and quality results.

The distinction that makes date-over-date comparison work (§17):

* ``CaptureLocation`` is a **place** on a floor plan (``P01``). It is stable across visits.
* ``CaptureFrame`` is a **photograph** taken at a place, during one session, at one instant.

So "compare 10 Sep with 03 Sep at P03" is a lookup of two frames sharing a ``location_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    CaptureSessionStatus,
    CaptureSource,
    DataProvenance,
    FileKind,
    MappingConfidence,
    ProjectionType,
    QualityVerdict,
)
from app.database.base import (
    Base,
    EnumType,
    JSONType,
    PKType,
    TimestampMixin,
    UTCDateTime,
    pk_column,
)

if TYPE_CHECKING:
    from app.models.annotation import Annotation
    from app.models.job import ProcessingJob
    from app.models.project import Floor, FloorPlan, Project, Zone
    from app.models.user import User


class CaptureSession(Base, TimestampMixin):
    """One operator, one floor, one visit (§33)."""

    __tablename__ = "capture_sessions"
    __table_args__ = (
        Index("ix_sessions_floor_started", "floor_id", "started_at"),
        Index("ix_sessions_project_started", "project_id", "started_at"),
        Index("ix_sessions_status", "status"),
    )

    #: Human-legible and sortable: ``CAP-20260910-103022-A8F3``.
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    building_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("buildings.id", ondelete="SET NULL"), nullable=True
    )
    floor_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("floors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    zone_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("zones.id", ondelete="SET NULL"), nullable=True
    )

    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[CaptureSessionStatus] = mapped_column(
        EnumType(CaptureSessionStatus, 32), nullable=False, default=CaptureSessionStatus.DRAFT
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    operator_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: §47 — human today, robot/drone/fixed camera later, without a schema change.
    capture_source: Mapped[CaptureSource] = mapped_column(
        EnumType(CaptureSource, 32), nullable=False, default=CaptureSource.HUMAN_OPERATOR
    )
    camera_device_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("camera_devices.id", ondelete="SET NULL"), nullable=True
    )
    camera_label: Mapped[str | None] = mapped_column(String(200), nullable=True)

    #: Per-session override of the video frame-extraction interval (§9).
    frame_interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: GPS fix at session start, when the device offered one.
    gps_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Filled by the MAPPING stage. Never fabricated — ``none`` means "we do not know" (§46).
    mapping_confidence: Mapped[MappingConfidence] = mapped_column(
        EnumType(MappingConfidence, 16), nullable=False, default=MappingConfidence.NONE
    )
    mapping_confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    mapping_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Denormalised counters kept current by the pipeline; they make dashboards cheap.
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    project: Mapped[Project] = relationship("Project", back_populates="capture_sessions")
    floor: Mapped[Floor] = relationship("Floor")
    zone: Mapped[Zone | None] = relationship("Zone")
    operator: Mapped[User | None] = relationship("User")
    files: Mapped[list[CaptureFile]] = relationship(
        "CaptureFile", back_populates="session", cascade="all, delete-orphan"
    )
    frames: Mapped[list[CaptureFrame]] = relationship(
        "CaptureFrame",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="CaptureFrame.sequence_index",
    )
    jobs: Mapped[list[ProcessingJob]] = relationship(
        "ProcessingJob", back_populates="session", cascade="all, delete-orphan"
    )
    path: Mapped[CapturePath | None] = relationship(
        "CapturePath", back_populates="session", cascade="all, delete-orphan", uselist=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CaptureSession {self.id} {self.status}>"


class CaptureFile(Base, TimestampMixin):
    """One uploaded artefact: a panorama, a flat photo, or a 360 video."""

    __tablename__ = "capture_files"
    __table_args__ = (Index("ix_capture_files_session", "session_id"),)

    id: Mapped[str] = pk_column()
    session_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(400), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: SHA-256 of the bytes. Lets a retried mobile upload be recognised instead of duplicated.
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    kind: Mapped[FileKind] = mapped_column(
        EnumType(FileKind, 32), nullable=False, default=FileKind.UNKNOWN
    )
    projection: Mapped[ProjectionType] = mapped_column(
        EnumType(ProjectionType, 32), nullable=False, default=ProjectionType.UNKNOWN
    )
    width_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)

    captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    camera_make: Mapped[str | None] = mapped_column(String(120), nullable=True)
    camera_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    gps_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_altitude_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Full extracted EXIF/XMP payload, kept verbatim for traceability.
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    validation_errors: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    validation_warnings: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)

    session: Mapped[CaptureSession] = relationship("CaptureSession", back_populates="files")
    frames: Mapped[list[CaptureFrame]] = relationship(
        "CaptureFrame", back_populates="file", cascade="all, delete-orphan"
    )


class CaptureFrame(Base, TimestampMixin):
    """One viewable panorama.

    The atom addressed by the viewer, the timeline, the compare view and annotations alike.
    """

    __tablename__ = "capture_frames"
    __table_args__ = (
        Index("ix_frames_session_seq", "session_id", "sequence_index"),
        Index("ix_frames_location_captured", "location_id", "captured_at"),
        UniqueConstraint("file_id", "sequence_index", name="uq_frame_file_sequence"),
    )

    id: Mapped[str] = pk_column()
    session_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("capture_files.id", ondelete="CASCADE"), nullable=False
    )
    #: 0-based order within the session's walk.
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Offset into the source video, or 0 for a still.
    source_time_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    thumbnail_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    preview_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)
    projection: Mapped[ProjectionType] = mapped_column(
        EnumType(ProjectionType, 32), nullable=False, default=ProjectionType.EQUIRECTANGULAR
    )
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)

    #: Where on the floor plan this frame was taken. NULL until placed (manually or by mapping).
    location_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("capture_locations.id", ondelete="SET NULL"), nullable=True
    )

    #: Scale-free relative offset from the session's first frame, in arbitrary units.
    #: Only meaningful together with ``position_provenance`` and the session's confidence.
    rel_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    rel_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_provenance: Mapped[DataProvenance | None] = mapped_column(
        EnumType(DataProvenance, 20), nullable=True
    )
    position_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Initial yaw the viewer should open at, degrees. Set by VIEWER_DATA.
    initial_yaw_deg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_keyframe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: ORB descriptors, serialised. Written by FEATURES, read by MAPPING and compare.
    feature_blob_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    feature_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 64-bit perceptual hash as hex, for near-duplicate detection.
    perceptual_hash: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    session: Mapped[CaptureSession] = relationship("CaptureSession", back_populates="frames")
    file: Mapped[CaptureFile] = relationship("CaptureFile", back_populates="frames")
    location: Mapped[CaptureLocation | None] = relationship(
        "CaptureLocation", back_populates="frames"
    )
    quality: Mapped[QualityResult | None] = relationship(
        "QualityResult", back_populates="frame", cascade="all, delete-orphan", uselist=False
    )
    annotations: Mapped[list[Annotation]] = relationship(
        "Annotation", back_populates="frame", cascade="all, delete-orphan"
    )


class CaptureLocation(Base, TimestampMixin):
    """A stable place on a floor plan (``P01``…), independent of any single visit (§13)."""

    __tablename__ = "capture_locations"
    __table_args__ = (
        UniqueConstraint("floor_id", "code", name="uq_location_floor_code"),
        Index("ix_locations_floor", "floor_id"),
    )

    id: Mapped[str] = pk_column()
    floor_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("floors.id", ondelete="CASCADE"), nullable=False
    )
    zone_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("zones.id", ondelete="SET NULL"), nullable=True
    )
    floor_plan_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("floor_plans.id", ondelete="SET NULL"), nullable=True
    )
    #: ``P01``, ``P02``, … unique per floor.
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)

    #: Pixel coordinates in the referenced floor-plan raster.
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    #: How these coordinates came to be — ``manual`` for the editor, ``estimated`` for mapping.
    provenance: Mapped[DataProvenance] = mapped_column(
        EnumType(DataProvenance, 20), nullable=False, default=DataProvenance.MANUAL
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_by_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    floor: Mapped[Floor] = relationship("Floor", back_populates="capture_locations")
    zone: Mapped[Zone | None] = relationship("Zone", back_populates="capture_locations")
    floor_plan: Mapped[FloorPlan | None] = relationship(
        "FloorPlan", back_populates="capture_locations"
    )
    frames: Mapped[list[CaptureFrame]] = relationship(
        "CaptureFrame", back_populates="location", order_by="CaptureFrame.captured_at"
    )


class CapturePath(Base, TimestampMixin):
    """The ordered walk through locations for one session (§13).

    Stored as an ordered list of ``{location_id, frame_id, timestamp}`` steps rather than a join
    table, because the path is read as a whole (to draw a polyline) and never queried by step.
    """

    __tablename__ = "capture_paths"

    id: Mapped[str] = pk_column()
    session_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("capture_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    floor_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("floors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: ``[{"location_id": ..., "frame_id": ..., "code": "P01", "x": 432, "y": 285,
    #:    "timestamp": "..."}, ...]`` ordered by timestamp.
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)
    provenance: Mapped[DataProvenance] = mapped_column(
        EnumType(DataProvenance, 20), nullable=False, default=DataProvenance.MANUAL
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    session: Mapped[CaptureSession] = relationship("CaptureSession", back_populates="path")


class QualityResult(Base, TimestampMixin):
    """Per-frame image-quality assessment (§10).

    These are classical CV measurements (variance of Laplacian, histogram statistics), not a
    learned model, and the documentation says so. No claim of equivalence to any vendor's
    proprietary quality algorithm is made.
    """

    __tablename__ = "quality_results"

    id: Mapped[str] = pk_column()
    frame_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("capture_frames.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    quality_score: Mapped[float] = mapped_column(Float, nullable=False)
    blur_score: Mapped[float] = mapped_column(Float, nullable=False)
    brightness_score: Mapped[float] = mapped_column(Float, nullable=False)
    contrast_score: Mapped[float] = mapped_column(Float, nullable=False)
    resolution_score: Mapped[float] = mapped_column(Float, nullable=False)
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False)

    verdict: Mapped[QualityVerdict] = mapped_column(EnumType(QualityVerdict, 16), nullable=False)
    is_usable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duplicate_of_frame_id: Mapped[str | None] = mapped_column(PKType, nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    #: Raw measurements behind the normalised scores, so a score is always auditable.
    raw_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    frame: Mapped[CaptureFrame] = relationship("CaptureFrame", back_populates="quality")
