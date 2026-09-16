"""The queue-side wrapper that executes one pipeline stage (§8, §34, §45).

This is the only place that knows how a stage failure becomes a failed capture: it opens a
database session, marks the ``ProcessingJob`` running, calls the stage, and records the outcome —
including advancing the capture's user-visible status. Keeping that in one function is what makes
every queue backend behave identically.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.enums import (
    PIPELINE_ORDER,
    CaptureSessionStatus,
    JobStatus,
    JobType,
)
from app.core.errors import ProcessingError, SiteScopeError
from app.core.logging import correlation_id, get_logger, log_event, new_correlation_id
from app.database.session import SessionLocal
from app.events import publish_capture_status
from app.models import CaptureSession, ProcessingJob
from app.processing.pipeline import mark_session_failed, mark_session_ready
from app.processing.stages import STAGE_HANDLERS, STAGE_SESSION_STATUS
from app.queue.base import JobSpec

logger = get_logger(__name__)


def handle_job(spec: JobSpec) -> dict[str, Any]:
    """Execute one stage. Raises on failure so the queue can stop the chain."""
    if correlation_id.get() is None:
        correlation_id.set(new_correlation_id())

    handler = STAGE_HANDLERS.get(spec.job_type)
    if handler is None:
        raise ProcessingError(f"No handler registered for stage {spec.job_type}")

    db = SessionLocal()
    started = time.monotonic()
    job: ProcessingJob | None = None
    try:
        session = db.get(CaptureSession, spec.session_id)
        if session is None:
            raise ProcessingError(f"Capture session {spec.session_id} no longer exists")

        job = _resolve_job(db, spec)
        if session.status is CaptureSessionStatus.CANCELLED:
            job.status = JobStatus.SKIPPED
            job.message = "Capture session was cancelled"
            job.finished_at = datetime.now(UTC)
            db.commit()
            return {"skipped": True, "reason": "cancelled"}

        job.status = JobStatus.RUNNING
        job.attempt += 1
        job.started_at = datetime.now(UTC)
        job.progress = 0.0
        job.error_code = None
        job.error_detail = None
        session.status = CaptureSessionStatus(STAGE_SESSION_STATUS[spec.job_type])
        db.commit()

        log_event(
            "processing.stage.started",
            session_id=session.id,
            stage=str(spec.job_type),
            attempt=job.attempt,
        )
        # After the commit, always. An event is an instruction to refetch, so announcing a
        # status the database has not yet been told about sends the client to read the previous
        # one -- and then nothing arrives to correct it until the next poll, which is precisely
        # the delay the push exists to remove.
        publish_capture_status(session.id, str(session.status), stage=str(spec.job_type))

        result = handler(db, session, spec)

        job.status = JobStatus.COMPLETE
        job.progress = 1.0
        job.finished_at = datetime.now(UTC)
        job.duration_seconds = round(time.monotonic() - started, 3)
        job.result = _truncate(result)
        job.message = None

        if _is_last_stage(db, session.id, spec.job_type):
            mark_session_ready(db, session)

        db.commit()
        log_event(
            "processing.stage.completed",
            session_id=session.id,
            stage=str(spec.job_type),
            duration_seconds=job.duration_seconds,
        )
        publish_capture_status(
            session.id, str(session.status), stage=str(spec.job_type), progress=1.0
        )
        return result

    except Exception as exc:
        db.rollback()
        message = exc.message if isinstance(exc, SiteScopeError) else str(exc)
        code = exc.code if isinstance(exc, SiteScopeError) else exc.__class__.__name__
        logger.exception(
            "Pipeline stage failed",
            extra={"stage": str(spec.job_type), "session_id": spec.session_id},
        )
        _record_failure(db, spec, code=code, message=message, started=started)
        log_event(
            "processing.failed",
            session_id=spec.session_id,
            stage=str(spec.job_type),
            error_code=code,
            error=message,
        )
        # The failure is the notification that matters most: it is the one the operator would
        # otherwise sit and wait through, watching a progress bar that has stopped moving.
        publish_capture_status(
            spec.session_id,
            str(CaptureSessionStatus.FAILED),
            stage=str(spec.job_type),
            error=message,
        )
        raise
    finally:
        db.close()


def _resolve_job(db: Any, spec: JobSpec) -> ProcessingJob:
    """Find the job row this execution belongs to, creating one if the spec has none.

    A spec without ``job_row_id`` happens when a stage is invoked directly (tests, a manual
    replay). Creating the row keeps the admin dashboard complete rather than silently missing
    executions.
    """
    if spec.job_row_id:
        job = db.get(ProcessingJob, spec.job_row_id)
        if job is not None:
            return job
    job = ProcessingJob(
        session_id=spec.session_id,
        file_id=spec.file_id,
        job_type=spec.job_type,
        status=JobStatus.QUEUED,
    )
    db.add(job)
    db.flush()
    return job


def _record_failure(
    db: Any,
    spec: JobSpec,
    *,
    code: str,
    message: str,
    started: float,
) -> None:
    try:
        job = _resolve_job(db, spec)
        job.status = JobStatus.FAILED
        job.finished_at = datetime.now(UTC)
        job.duration_seconds = round(time.monotonic() - started, 3)
        job.error_code = code
        job.error_detail = message[:4000]
        job.message = message[:500]

        session = db.get(CaptureSession, spec.session_id)
        if session is not None:
            mark_session_failed(db, session, f"{spec.job_type}: {message}"[:2000])
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not record pipeline failure")


def _is_last_stage(db: Any, session_id: str, stage: JobType) -> bool:
    """True when nothing else is queued or running for this session.

    Checked from the database rather than by comparing against the last element of
    ``PIPELINE_ORDER``, because a partial re-run (say, QUALITY only) must also finish as
    ``ready`` when its own chain drains.
    """
    if stage is PIPELINE_ORDER[-1]:
        return True
    pending = db.scalar(
        select(ProcessingJob.id)
        .where(
            ProcessingJob.session_id == session_id,
            ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            ProcessingJob.job_type != stage,
        )
        .limit(1)
    )
    return pending is None


def _truncate(result: dict[str, Any], limit: int = 200) -> dict[str, Any]:
    """Keep job results readable. A 600-frame mapping result should not be a 2 MB JSON blob."""
    trimmed: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, list) and len(value) > limit:
            trimmed[key] = value[:limit]
            trimmed[f"{key}_truncated_from"] = len(value)
        else:
            trimmed[key] = value
    return trimmed


def run_pipeline_inline(
    session_id: str, stages: list[JobType] | None = None
) -> list[dict[str, Any]]:
    """Run the pipeline synchronously, in order, in the calling thread.

    For the seeder, CLI tools and tests — never for a request handler, which must not block on
    image processing (§8). It creates the same ``ProcessingJob`` rows and calls the same handler
    as the queue, so what it exercises is the real pipeline and not a parallel implementation.
    """
    from app.database.session import SessionLocal as _SessionLocal

    ordered = [stage for stage in PIPELINE_ORDER if stages is None or stage in set(stages)]

    db = _SessionLocal()
    try:
        session = db.get(CaptureSession, session_id)
        if session is None:
            raise ProcessingError(f"Capture session {session_id} not found")
        session.status = CaptureSessionStatus.PROCESSING
        session.failure_reason = None

        specs: list[JobSpec] = []
        for stage in ordered:
            job = ProcessingJob(session_id=session_id, job_type=stage, status=JobStatus.QUEUED)
            db.add(job)
            db.flush()
            specs.append(
                JobSpec(
                    job_type=stage,
                    session_id=session_id,
                    job_row_id=job.id,
                    payload=(
                        {"frame_interval_seconds": session.frame_interval_seconds}
                        if stage is JobType.FRAME_EXTRACTION and session.frame_interval_seconds
                        else {}
                    ),
                )
            )
        db.commit()
    finally:
        db.close()

    results: list[dict[str, Any]] = []
    for spec in specs:
        results.append(handle_job(spec))
    return results


# Register with the thread backend at import time.
from app.queue.thread import set_handler  # noqa: E402

set_handler(handle_job)
