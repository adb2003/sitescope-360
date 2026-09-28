"""Report generation routes (§29)."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import Response
from sqlalchemy import func, select

from app.api.deps import (
    DbSession,
    Pagination,
    RequireProjectRead,
    RequireReportGenerate,
    get_project_or_404,
    get_report_or_404,
)
from app.core.enums import ReportFormat, ReportStatus, ReportType
from app.core.errors import NotFoundError, SiteScopeError
from app.core.ids import new_id
from app.core.logging import get_logger, log_event
from app.models import Report, User
from app.reports.builders import build_report
from app.reports.render import render
from app.schemas.common import Message, Page
from app.schemas.report import ReportCreate, ReportDetail, ReportOut
from app.services.assets import sign
from app.storage import get_storage, report_key

logger = get_logger(__name__)
router = APIRouter(tags=["reports"])

_EXTENSIONS = {
    ReportFormat.PDF: ".pdf",
    ReportFormat.CSV: ".csv",
    ReportFormat.JSON: ".json",
}


@router.post(
    "/projects/{project_id}/reports",
    response_model=ReportDetail,
    status_code=status.HTTP_201_CREATED,
)
def generate_report(
    project_id: str,
    payload: ReportCreate,
    db: DbSession,
    user: RequireReportGenerate,
) -> ReportDetail:
    """Build and render a report synchronously.

    Reports are database aggregations plus a layout pass — hundreds of milliseconds, not minutes —
    so queueing them would add a polling round trip for no benefit. The pipeline's queue exists
    for work that decodes video; this is not that.
    """
    project = get_project_or_404(db, project_id, user)

    record = Report(
        id=new_id(),
        project_id=project.id,
        floor_id=payload.floor_id,
        report_type=payload.report_type,
        report_format=payload.report_format,
        status=ReportStatus.GENERATING,
        title=payload.title or _default_title(payload.report_type, project.name),
        period_start=payload.period_start,
        period_end=payload.period_end,
        requested_by_id=user.id,
    )
    db.add(record)
    db.commit()

    try:
        data = build_report(
            db,
            report_type=payload.report_type,
            project_id=project.id,
            floor_id=payload.floor_id,
            period_start=payload.period_start,
            period_end=payload.period_end,
        )
        if payload.title:
            data["title"] = payload.title
        content, content_type = render(data, payload.report_format)

        key = report_key(project.id, f"{record.id}{_EXTENSIONS[payload.report_format]}")
        get_storage().put_bytes(key, content, content_type)

        record.storage_key = key
        record.file_size_bytes = len(content)
        # Persisted so the same numbers can be re-rendered in another format without recomputing.
        record.payload = data
        record.status = ReportStatus.READY
        record.generated_at = datetime.now(UTC)
        record.title = data.get("title", record.title)
        db.commit()

    except SiteScopeError as exc:
        record.status = ReportStatus.FAILED
        record.error_detail = exc.message
        db.commit()
        raise
    except Exception as exc:
        logger.exception("Report generation failed", extra={"report_id": record.id})
        record.status = ReportStatus.FAILED
        record.error_detail = str(exc)[:2000]
        db.commit()
        raise

    log_event(
        "report.generated",
        report_id=record.id,
        project_id=project.id,
        report_type=str(record.report_type),
        report_format=str(record.report_format),
        size_bytes=record.file_size_bytes,
    )
    return _detail(record, include_payload=False)


@router.get("/projects/{project_id}/reports", response_model=Page[ReportOut])
def list_reports(
    project_id: str,
    db: DbSession,
    page: Pagination,
    user: RequireProjectRead,
    report_type: Annotated[ReportType | None, Query()] = None,
) -> Page[ReportOut]:
    get_project_or_404(db, project_id, user)
    query = select(Report).where(Report.project_id == project_id)
    if report_type:
        query = query.where(Report.report_type == report_type)

    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = db.scalars(
        query.order_by(Report.created_at.desc()).limit(page.limit).offset(page.offset)
    )
    return Page(
        items=[_out(row) for row in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/reports/{report_id}", response_model=ReportDetail)
def get_report(report_id: str, db: DbSession, user: RequireProjectRead) -> ReportDetail:
    return _detail(_get_or_404(db, report_id, user), include_payload=True)


@router.get("/reports/{report_id}/download")
def download_report(report_id: str, db: DbSession, user: RequireProjectRead) -> Response:
    """Stream the rendered report with a filename, for a browser download."""
    record = _get_or_404(db, report_id, user)
    if record.status is not ReportStatus.READY or not record.storage_key:
        raise NotFoundError(f"Report is {record.status} and has no file to download")

    content = get_storage().read_bytes(record.storage_key)
    content_type = {
        ReportFormat.PDF: "application/pdf",
        ReportFormat.CSV: "text/csv",
        ReportFormat.JSON: "application/json",
    }[record.report_format]

    safe_title = (
        "".join(char if char.isalnum() or char in " -_" else "_" for char in record.title).strip()
        or "report"
    )
    filename = f"{safe_title}{_EXTENSIONS[record.report_format]}"

    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/reports/{report_id}", response_model=Message)
def delete_report(report_id: str, db: DbSession, user: RequireReportGenerate) -> Message:
    record = _get_or_404(db, report_id, user)
    if record.storage_key:
        with contextlib.suppress(Exception):
            get_storage().delete(record.storage_key)
    db.delete(record)
    db.commit()
    return Message(message="Report deleted")


@router.get("/report-types")
def list_report_types(_: RequireProjectRead) -> dict:
    """What each report contains — used to build the report picker."""
    return {
        "types": [
            {
                "key": str(ReportType.PROJECT_SUMMARY),
                "name": "Project summary",
                "description": (
                    "Totals, coverage by floor, capture health, quality breakdown, capture "
                    "frequency, timeline and locations needing attention."
                ),
            },
            {
                "key": str(ReportType.FLOOR_COVERAGE),
                "name": "Floor coverage",
                "description": "Zone-by-zone coverage with declared and captured points.",
            },
            {
                "key": str(ReportType.CAPTURE_HISTORY),
                "name": "Capture history",
                "description": (
                    "Every capture session with date, operator, floor, zone, frame count, "
                    "quality and mapping confidence."
                ),
            },
            {
                "key": str(ReportType.ANNOTATION_LOG),
                "name": "Annotation log",
                "description": "All annotations with priority, status and resolution.",
            },
        ],
        "formats": [str(item) for item in ReportFormat],
    }


def _get_or_404(db: DbSession, report_id: str, user: User) -> Report:
    """Load a report and check the caller can reach its project (§27).

    A report is addressed by its own id, so nothing on the path resolves a project. It also
    carries the numbers rather than pointing at them — a leaked report id is the figures, not a
    route to them.
    """
    return get_report_or_404(db, report_id, user)


def _out(record: Report) -> ReportOut:
    return ReportOut(
        **{
            field: getattr(record, field)
            for field in (
                "id",
                "project_id",
                "floor_id",
                "report_type",
                "report_format",
                "status",
                "title",
                "period_start",
                "period_end",
                "file_size_bytes",
                "error_detail",
                "generated_at",
                "created_at",
            )
        },
        asset=sign(record.storage_key),
    )


def _detail(record: Report, *, include_payload: bool) -> ReportDetail:
    return ReportDetail(
        **_out(record).model_dump(),
        payload=record.payload if include_payload else None,
    )


def _default_title(report_type: ReportType, project_name: str) -> str:
    labels = {
        ReportType.PROJECT_SUMMARY: "Project summary",
        ReportType.FLOOR_COVERAGE: "Floor coverage",
        ReportType.CAPTURE_HISTORY: "Capture history",
        ReportType.ANNOTATION_LOG: "Annotation log",
    }
    return f"{labels[report_type]} — {project_name}"
