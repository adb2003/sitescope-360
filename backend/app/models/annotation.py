"""Annotations pinned to a point inside a 360 frame (§28) — the seed of defect management."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AnnotationPriority, AnnotationStatus
from app.database.base import Base, EnumType, PKType, TimestampMixin, UTCDateTime, pk_column
from app.models.capture import CaptureFrame
from app.models.user import User


class Annotation(Base, TimestampMixin):
    __tablename__ = "annotations"
    __table_args__ = (
        Index("ix_annotations_frame", "frame_id"),
        Index("ix_annotations_status_priority", "status", "priority"),
    )

    id: Mapped[str] = pk_column()
    frame_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("capture_frames.id", ondelete="CASCADE"), nullable=False
    )
    #: Denormalised for cheap project-wide annotation reports.
    project_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    floor_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("floors.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Position inside the panorama, in spherical degrees — resolution-independent, so an
    #: annotation stays put if the same frame is later re-encoded at another size.
    yaw_deg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pitch_deg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[AnnotationPriority] = mapped_column(
        EnumType(AnnotationPriority, 16), nullable=False, default=AnnotationPriority.MEDIUM
    )
    status: Mapped[AnnotationStatus] = mapped_column(
        EnumType(AnnotationStatus, 20), nullable=False, default=AnnotationStatus.OPEN
    )

    created_by_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_to_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    frame: Mapped[CaptureFrame] = relationship("CaptureFrame", back_populates="annotations")
    created_by: Mapped[User | None] = relationship("User", foreign_keys=[created_by_id])
    assigned_to: Mapped[User | None] = relationship("User", foreign_keys=[assigned_to_id])
