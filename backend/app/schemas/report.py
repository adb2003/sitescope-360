from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from app.core.enums import ReportFormat, ReportStatus, ReportType
from app.schemas.common import ORMModel, SignedAsset


class ReportCreate(BaseModel):
    report_type: ReportType
    report_format: ReportFormat = ReportFormat.PDF
    floor_id: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    title: str | None = None


class ReportOut(ORMModel):
    id: str
    project_id: str
    floor_id: str | None
    report_type: ReportType
    report_format: ReportFormat
    status: ReportStatus
    title: str
    period_start: date | None
    period_end: date | None
    file_size_bytes: int | None
    error_detail: str | None
    generated_at: datetime | None
    created_at: datetime
    asset: SignedAsset | None = None


class ReportDetail(ReportOut):
    payload: dict[str, Any] | None = None
