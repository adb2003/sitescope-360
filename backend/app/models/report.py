"""Generated reports (§29)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Date, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ReportFormat, ReportStatus, ReportType
from app.database.base import (
    Base,
    EnumType,
    JSONType,
    PKType,
    TimestampMixin,
    UTCDateTime,
    pk_column,
)


class Report(Base, TimestampMixin):
    __tablename__ = "reports"
    __table_args__ = (Index("ix_reports_project_created", "project_id", "created_at"),)

    id: Mapped[str] = pk_column()
    project_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    floor_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("floors.id", ondelete="SET NULL"), nullable=True
    )
    report_type: Mapped[ReportType] = mapped_column(EnumType(ReportType, 40), nullable=False)
    report_format: Mapped[ReportFormat] = mapped_column(EnumType(ReportFormat, 10), nullable=False)
    status: Mapped[ReportStatus] = mapped_column(
        EnumType(ReportStatus, 20), nullable=False, default=ReportStatus.QUEUED
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: The computed report payload, kept so the same numbers can be re-rendered in another format.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    requested_by_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
