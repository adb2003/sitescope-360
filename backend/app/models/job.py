"""Processing jobs — one row per pipeline stage attempt (§8, §34, §45).

Persisting stage state in the database (rather than only in the queue) means the admin processing
dashboard, the per-capture status endpoint, and retry all work identically whichever queue backend
is configured, and survive a worker restart.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import JobStatus, JobType
from app.database.base import (
    Base,
    EnumType,
    JSONType,
    PKType,
    TimestampMixin,
    UTCDateTime,
    pk_column,
)
from app.models.capture import CaptureSession


class ProcessingJob(Base, TimestampMixin):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        Index("ix_jobs_session_type", "session_id", "job_type"),
        Index("ix_jobs_status_created", "status", "created_at"),
        # ON DELETE CASCADE from `capture_files`, on a table that gains a row per stage per
        # capture. Without this, deleting one uploaded file scans every job ever run.
        Index("ix_jobs_file", "file_id"),
    )

    id: Mapped[str] = pk_column()
    session_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("capture_files.id", ondelete="CASCADE"), nullable=True
    )
    job_type: Mapped[JobType] = mapped_column(EnumType(JobType, 40), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        EnumType(JobStatus, 20), nullable=False, default=JobStatus.QUEUED, index=True
    )

    #: Id handed back by the queue backend (RQ job id, or a synthetic id for ThreadQueue).
    queue_job_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    session: Mapped[CaptureSession] = relationship("CaptureSession", back_populates="jobs")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ProcessingJob {self.job_type} {self.status} {self.session_id}>"
