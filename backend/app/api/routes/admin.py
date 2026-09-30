"""Admin processing dashboard, system capabilities, and health (§45, §46)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import (
    DbSession,
    Pagination,
    RequireCaptureRead,
    RequireJobAdmin,
    get_session_or_404,
)
from app.bim.interfaces import bim_status
from app.core.audit import EVENT_NAMES
from app.core.config import settings
from app.core.enums import JobStatus, JobType
from app.core.errors import ConflictError, NotFoundError
from app.models import CaptureSession, ProcessingJob
from app.processing.pipeline import enqueue_pipeline
from app.queue import get_queue
from app.schemas.capture import ProcessingJobOut
from app.schemas.common import Page
from app.storage import get_storage
from app.vision.ai import get_ai_engine

router = APIRouter(tags=["admin"])


@router.get("/jobs", response_model=Page[ProcessingJobOut])
def list_jobs(
    db: DbSession,
    page: Pagination,
    _: RequireJobAdmin,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    job_type: Annotated[JobType | None, Query(alias="type")] = None,
    session_id: Annotated[str | None, Query()] = None,
) -> Page[ProcessingJobOut]:
    """The §45 processing dashboard, read straight from ``processing_jobs``."""
    query = select(ProcessingJob)
    if job_status:
        query = query.where(ProcessingJob.status == job_status)
    if job_type:
        query = query.where(ProcessingJob.job_type == job_type)
    if session_id:
        query = query.where(ProcessingJob.session_id == session_id)

    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = db.scalars(
        query.order_by(ProcessingJob.created_at.desc()).limit(page.limit).offset(page.offset)
    )
    return Page(
        items=[ProcessingJobOut.model_validate(row) for row in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/jobs/summary")
def job_summary(db: DbSession, _: RequireJobAdmin) -> dict:
    """Counts by status and by stage, plus recent failures and queue health."""
    by_status = {
        str(status): int(count)
        for status, count in db.execute(
            select(ProcessingJob.status, func.count(ProcessingJob.id)).group_by(
                ProcessingJob.status
            )
        )
    }
    by_type = {
        str(job_type): int(count)
        for job_type, count in db.execute(
            select(ProcessingJob.job_type, func.count(ProcessingJob.id)).group_by(
                ProcessingJob.job_type
            )
        )
    }
    mean_durations = {
        str(job_type): round(float(mean), 3)
        for job_type, mean in db.execute(
            select(ProcessingJob.job_type, func.avg(ProcessingJob.duration_seconds))
            .where(ProcessingJob.status == JobStatus.COMPLETE)
            .group_by(ProcessingJob.job_type)
        )
        if mean is not None
    }
    since = datetime.now(UTC) - timedelta(days=7)
    failures = [
        {
            "job_id": job.id,
            "session_id": job.session_id,
            "stage": str(job.job_type),
            "error_code": job.error_code,
            "message": job.message,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        }
        for job in db.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.status == JobStatus.FAILED, ProcessingJob.created_at >= since)
            .order_by(ProcessingJob.created_at.desc())
            .limit(25)
        )
    ]

    return {
        "by_status": by_status,
        "by_stage": by_type,
        "mean_duration_seconds": mean_durations,
        "recent_failures": failures,
        "queue": get_queue().health(),
    }


@router.post("/jobs/{job_id}/retry", response_model=list[ProcessingJobOut])
def retry_job(job_id: str, db: DbSession, user: RequireJobAdmin) -> list[ProcessingJobOut]:
    """Re-run a failed stage and everything after it.

    Re-running only the failed stage would leave downstream stages holding stale output, so the
    retry re-enqueues from the failure point onward.
    """
    job = db.get(ProcessingJob, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found")
    if job.status not in (JobStatus.FAILED, JobStatus.SKIPPED):
        raise ConflictError(f"Job is {job.status}; only failed or skipped jobs can be retried")

    from app.core.enums import PIPELINE_ORDER

    start = PIPELINE_ORDER.index(job.job_type)
    session = get_session_or_404(db, job.session_id, user)
    jobs = enqueue_pipeline(db, session, stages=list(PIPELINE_ORDER[start:]), force=True)
    return [ProcessingJobOut.model_validate(item) for item in jobs]


@router.get("/system/capabilities")
def system_capabilities(_: RequireCaptureRead) -> dict:
    """What is real, what is estimated, and what is not implemented (§46).

    The frontend reads this instead of hard-coding assumptions, so a disabled or unimplemented
    capability is never presented as working.
    """
    ai = get_ai_engine()
    return {
        "vision": ai.capabilities(),
        "bim": bim_status(),
        "provenance_levels": ["measured", "estimated", "ai_predicted", "manual"],
        "mapping_confidence_threshold": settings.mapping_confidence_threshold,
        "processing": {
            "queue_backend": settings.queue_backend,
            "default_frame_interval_seconds": settings.frame_interval_seconds,
            "max_frames_per_video": settings.max_frames_per_video,
        },
        "uploads": {
            "max_bytes": settings.max_upload_bytes,
            "image_extensions": settings.allowed_image_extensions,
            "video_extensions": settings.allowed_video_extensions,
            "floorplan_extensions": settings.allowed_floorplan_extensions,
        },
        "storage_backend": settings.storage_backend,
        "independence": {
            "third_party_monitoring_platforms": [],
            "statement": (
                "This platform uses no OpenSpace (or other proprietary construction-monitoring "
                "vendor) API, SDK, application, account or code. All capture, processing, "
                "viewing and analysis is implemented in this repository on open-source "
                "components."
            ),
        },
    }


@router.get("/system/health")
def system_health(db: DbSession) -> dict:
    """Unauthenticated liveness probe for Docker and load balancers."""
    database_ok = True
    error: str | None = None
    try:
        db.execute(select(1))
    except Exception as exc:
        database_ok = False
        error = str(exc)

    queue_health = get_queue().health()

    # Storage used to be reported as `settings.storage_backend` — the *name* of the configured
    # backend, which is true whether or not the bucket exists. A capture platform whose object
    # store is unreachable cannot serve an image or accept an upload, so a probe that answered
    # "ok" through that was not reporting health; it was suppressing the alarm.
    #
    # Wrapped, because a liveness probe must not 500. An exception escaping here would tell a
    # load balancer the whole application is broken, when the truth is narrower and more useful.
    try:
        storage_health = get_storage().health()
    except Exception as exc:  # pragma: no cover - the providers already return rather than raise
        storage_health = {"backend": settings.storage_backend, "ok": False, "error": str(exc)}

    storage_ok = bool(storage_health.get("ok", False))
    return {
        # Degraded, not failed, when storage is down: the API is still answering and the
        # distinction is what tells an operator which of the two to go and look at.
        "status": "ok" if database_ok and storage_ok else "degraded",
        "app": settings.app_name,
        "environment": settings.environment,
        "database": {"ok": database_ok, "error": error},
        "queue": queue_health,
        "storage": storage_health,
    }


@router.get("/system/events")
def recent_events(
    db: DbSession,
    _: RequireJobAdmin,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    """Recent capture-lifecycle activity, derived from persisted state.

    Structured logs (§45) are the event stream; this endpoint reads the durable record instead,
    so the admin view works without a log aggregator attached.
    """
    sessions = list(
        db.scalars(select(CaptureSession).order_by(CaptureSession.updated_at.desc()).limit(limit))
    )
    return {
        "sessions": [
            {
                "session_id": item.id,
                "status": str(item.status),
                "project_id": item.project_id,
                "floor_id": item.floor_id,
                "frame_count": item.frame_count,
                "mapping_confidence": str(item.mapping_confidence),
                "failure_reason": item.failure_reason,
                "updated_at": item.updated_at.isoformat(),
            }
            for item in sessions
        ],
        # Read from `core.audit`, not written out here. This was a hard-coded list of eight
        # while the code emitted thirty-five — a false statement about the system, inside the
        # endpoint whose job is to describe it. A unit test holds the registry to the code.
        "logged_event_names": list(EVENT_NAMES),
    }
