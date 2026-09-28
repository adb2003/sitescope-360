"""Pipeline orchestration (§8, §34).

Responsibilities kept deliberately narrow:

* create one ``ProcessingJob`` row per stage the caller asked for;
* enqueue them as a dependent chain so a failed stage stops the rest;
* answer "what is the status of this capture" from those rows.

Stage *execution* lives in :mod:`app.processing.stages`; the queue-side wrapper that runs a stage
and records its outcome lives in :mod:`app.workers.handlers`. Nothing here imports a queue
backend directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    PIPELINE_ORDER,
    CaptureSessionStatus,
    JobStatus,
    JobType,
)
from app.core.errors import ConflictError, ValidationError
from app.core.logging import log_event
from app.events import publish_capture_status
from app.models import CaptureFile, CaptureSession, ProcessingJob
from app.queue import get_queue
from app.queue.base import JobSpec


def enqueue_pipeline(
    db: Session,
    session: CaptureSession,
    *,
    stages: list[JobType] | None = None,
    frame_interval_seconds: float | None = None,
    force: bool = False,
) -> list[ProcessingJob]:
    """Queue the pipeline for one capture session.

    ``force`` re-runs stages that already completed — the behaviour behind the retry button.
    Without it, an already-completed stage is skipped so a retry after a late failure does not
    redo the expensive early work.
    """
    if not _has_files(db, session):
        raise ValidationError(
            "Upload at least one file before processing this capture session",
            session_id=session.id,
        )

    requested = list(stages) if stages else list(PIPELINE_ORDER)
    unknown = [stage for stage in requested if stage not in PIPELINE_ORDER]
    if unknown:
        raise ValidationError(f"Unknown pipeline stages: {[str(s) for s in unknown]}")
    # Always run in canonical order regardless of how the caller listed them; a later stage
    # reading an earlier stage's output is the whole point of the sequence.
    ordered = [stage for stage in PIPELINE_ORDER if stage in set(requested)]

    active = _active_jobs(db, session.id)
    if active and not force:
        raise ConflictError(
            "This capture is already being processed",
            session_id=session.id,
            active_stages=[str(job.job_type) for job in active],
        )

    if frame_interval_seconds is not None:
        session.frame_interval_seconds = frame_interval_seconds

    completed = _completed_stages(db, session.id) if not force else set()
    to_run = [stage for stage in ordered if stage not in completed]
    if not to_run:
        raise ConflictError(
            "Every requested stage has already completed. Use force=true to re-run.",
            session_id=session.id,
        )

    jobs: list[ProcessingJob] = []
    for stage in to_run:
        job = ProcessingJob(
            session_id=session.id,
            job_type=stage,
            status=JobStatus.QUEUED,
            attempt=0,
        )
        db.add(job)
        jobs.append(job)

    session.status = CaptureSessionStatus.PROCESSING
    session.failure_reason = None
    db.flush()

    specs = [
        JobSpec(
            job_type=job.job_type,
            session_id=session.id,
            job_row_id=job.id,
            payload=(
                {"frame_interval_seconds": session.frame_interval_seconds}
                if job.job_type is JobType.FRAME_EXTRACTION and session.frame_interval_seconds
                else {}
            ),
        )
        for job in jobs
    ]

    # Commit before enqueuing: a worker may pick the job up immediately, and it must be able to
    # see the rows. With the in-process thread queue this is not merely a race — the worker
    # thread shares the file/database and would read a half-written transaction.
    db.commit()

    queue = get_queue()
    queue_ids = queue.enqueue_chain(specs)
    for job, queue_id in zip(jobs, queue_ids, strict=False):
        job.queue_job_id = queue_id
    db.commit()

    log_event(
        "processing.started",
        session_id=session.id,
        stages=[str(stage) for stage in to_run],
        forced=force,
    )
    # The first notification, and the one that makes the status page useful immediately: the
    # operator pressed "process" in one tab and the capture list in another now says so without
    # waiting out a poll interval.
    publish_capture_status(
        session.id, str(session.status), project_id=session.project_id, progress=0.0
    )
    return jobs


def pipeline_progress(db: Session, session_id: str) -> tuple[float, list[ProcessingJob]]:
    """Fraction of the canonical pipeline that has completed, plus the stage rows."""
    jobs = list(
        db.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.session_id == session_id)
            .order_by(ProcessingJob.created_at)
        )
    )
    if not jobs:
        return 0.0, []

    # Score the latest attempt of each stage, so a retried stage does not count twice.
    latest: dict[JobType, ProcessingJob] = {}
    for job in jobs:
        current = latest.get(job.job_type)
        if current is None or job.created_at >= current.created_at:
            latest[job.job_type] = job

    total = len(PIPELINE_ORDER)
    score = 0.0
    for stage in PIPELINE_ORDER:
        stage_job = latest.get(stage)
        if stage_job is None:
            continue
        if stage_job.status in (JobStatus.COMPLETE, JobStatus.SKIPPED):
            score += 1.0
        elif stage_job.status is JobStatus.RUNNING:
            score += max(0.0, min(1.0, stage_job.progress))
    return round(score / total, 4), jobs


def mark_session_failed(db: Session, session: CaptureSession, reason: str) -> None:
    session.status = CaptureSessionStatus.FAILED
    session.failure_reason = reason
    db.flush()


def mark_session_ready(db: Session, session: CaptureSession) -> None:
    session.status = CaptureSessionStatus.READY
    session.failure_reason = None
    if session.ended_at is None:
        session.ended_at = datetime.now(UTC)
    db.flush()
    log_event("processing.completed", session_id=session.id, frame_count=session.frame_count)


def _has_files(db: Session, session: CaptureSession) -> bool:
    return (
        db.scalar(select(CaptureFile.id).where(CaptureFile.session_id == session.id).limit(1))
        is not None
    )


def _active_jobs(db: Session, session_id: str) -> list[ProcessingJob]:
    return list(
        db.scalars(
            select(ProcessingJob).where(
                ProcessingJob.session_id == session_id,
                ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
    )


def _completed_stages(db: Session, session_id: str) -> set[JobType]:
    rows = db.scalars(
        select(ProcessingJob.job_type).where(
            ProcessingJob.session_id == session_id,
            ProcessingJob.status.in_([JobStatus.COMPLETE, JobStatus.SKIPPED]),
        )
    )
    return {JobType(row) for row in rows}
