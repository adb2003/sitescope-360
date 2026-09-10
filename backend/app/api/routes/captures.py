"""Capture session, upload, processing, frame and viewer routes (§26, §34)."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile, status
from sqlalchemy import func, select

from app.api.deps import (
    DbSession,
    Pagination,
    RequireCaptureCreate,
    RequireCaptureDelete,
    RequireCaptureRead,
    RequireLocationEdit,
    RequireUpload,
    get_frame_or_404,
    get_project_or_404,
    get_session_or_404,
)
from app.core.enums import (
    CaptureSessionStatus,
    QualityVerdict,
)
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    Building,
    CaptureFile,
    CaptureFrame,
    CaptureLocation,
    CapturePath,
    CaptureSession,
    Floor,
    ProcessingJob,
    QualityResult,
    User,
    Zone,
)
from app.processing.pipeline import enqueue_pipeline, pipeline_progress
from app.schemas.capture import (
    CaptureFileDetail,
    CaptureFileOut,
    CaptureFrameDetail,
    CapturePathOut,
    CaptureSessionCreate,
    CaptureSessionDetail,
    CaptureSessionOut,
    CaptureSessionUpdate,
    FrameAssignLocation,
    ProcessingJobOut,
    ProcessingStatus,
    ProcessRequest,
    ViewerManifest,
)
from app.schemas.common import Message, Page
from app.services import captures as capture_service
from app.services import viewer as viewer_service
from app.services.assets import sign

router = APIRouter(tags=["captures"])


# ---- sessions ----------------------------------------------------------------
@router.post("/captures", response_model=CaptureSessionOut, status_code=status.HTTP_201_CREATED)
def create_capture_session(
    payload: CaptureSessionCreate, db: DbSession, user: RequireCaptureCreate
) -> CaptureSessionOut:
    session = capture_service.create_session(db, payload, user)
    return CaptureSessionOut.model_validate(session)


@router.get("/captures", response_model=Page[CaptureSessionOut])
def list_capture_sessions(
    db: DbSession,
    page: Pagination,
    _: RequireCaptureRead,
    project_id: Annotated[str | None, Query()] = None,
    floor_id: Annotated[str | None, Query()] = None,
    session_status: Annotated[CaptureSessionStatus | None, Query(alias="status")] = None,
    since: Annotated[
        datetime | None, Query(description="Only sessions started at or after")
    ] = None,
) -> Page[CaptureSessionOut]:
    query = select(CaptureSession)
    if project_id:
        query = query.where(CaptureSession.project_id == project_id)
    if floor_id:
        query = query.where(CaptureSession.floor_id == floor_id)
    if session_status:
        query = query.where(CaptureSession.status == session_status)
    if since:
        query = query.where(CaptureSession.started_at >= since)

    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = db.scalars(
        query.order_by(CaptureSession.started_at.desc()).limit(page.limit).offset(page.offset)
    )
    return Page(
        items=[CaptureSessionOut.model_validate(row) for row in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/captures/{session_id}", response_model=CaptureSessionDetail)
def get_capture_session(
    session_id: str, db: DbSession, _: RequireCaptureRead
) -> CaptureSessionDetail:
    session = get_session_or_404(db, session_id)
    row = db.execute(
        select(Floor.name, Building.name, Zone.name, User.full_name)
        .select_from(CaptureSession)
        .join(Floor, Floor.id == CaptureSession.floor_id)
        .join(Building, Building.id == Floor.building_id)
        .outerjoin(Zone, Zone.id == CaptureSession.zone_id)
        .outerjoin(User, User.id == CaptureSession.operator_id)
        .where(CaptureSession.id == session_id)
    ).first()
    floor_name, building_name, zone_name, operator_name = row or (None, None, None, None)

    files = list(
        db.scalars(
            select(CaptureFile)
            .where(CaptureFile.session_id == session_id)
            .order_by(CaptureFile.created_at)
        )
    )
    jobs = list(
        db.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.session_id == session_id)
            .order_by(ProcessingJob.created_at)
        )
    )
    verdict_rows = db.execute(
        select(QualityResult.verdict, func.count(QualityResult.id))
        .join(CaptureFrame, CaptureFrame.id == QualityResult.frame_id)
        .where(CaptureFrame.session_id == session_id)
        .group_by(QualityResult.verdict)
    )
    quality_summary = {str(verdict): int(count) for verdict, count in verdict_rows}

    project = get_project_or_404(db, session.project_id)
    return CaptureSessionDetail(
        **CaptureSessionOut.model_validate(session).model_dump(),
        project_name=project.name,
        floor_name=floor_name,
        building_name=building_name,
        zone_name=zone_name,
        operator_name=operator_name,
        files=[CaptureFileOut.model_validate(item) for item in files],
        jobs=[ProcessingJobOut.model_validate(item) for item in jobs],
        quality_summary=quality_summary,
    )


@router.patch("/captures/{session_id}", response_model=CaptureSessionOut)
def update_capture_session(
    session_id: str, payload: CaptureSessionUpdate, db: DbSession, _: RequireCaptureCreate
) -> CaptureSessionOut:
    session = get_session_or_404(db, session_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(session, field, value)
    db.commit()
    return CaptureSessionOut.model_validate(session)


@router.post("/captures/{session_id}/finish", response_model=CaptureSessionOut)
def finish_capture_session(
    session_id: str, db: DbSession, _: RequireCaptureCreate
) -> CaptureSessionOut:
    """Close the recording window. Does not start processing — that is an explicit step."""
    session = get_session_or_404(db, session_id)
    if session.ended_at is None:
        session.ended_at = datetime.now(UTC)
    if session.status in (CaptureSessionStatus.DRAFT, CaptureSessionStatus.RECORDING):
        has_files = db.scalar(
            select(CaptureFile.id).where(CaptureFile.session_id == session_id).limit(1)
        )
        session.status = CaptureSessionStatus.UPLOADED if has_files else CaptureSessionStatus.DRAFT
    db.commit()
    return CaptureSessionOut.model_validate(session)


@router.post("/captures/{session_id}/cancel", response_model=CaptureSessionOut)
def cancel_capture_session(
    session_id: str, db: DbSession, _: RequireCaptureCreate
) -> CaptureSessionOut:
    session = get_session_or_404(db, session_id)
    if session.status is CaptureSessionStatus.READY:
        raise ConflictError("A completed capture cannot be cancelled; delete it instead")
    session.status = CaptureSessionStatus.CANCELLED
    session.ended_at = session.ended_at or datetime.now(UTC)
    db.commit()
    return CaptureSessionOut.model_validate(session)


@router.delete("/captures/{session_id}", response_model=Message)
def delete_capture_session(session_id: str, db: DbSession, _: RequireCaptureDelete) -> Message:
    session = get_session_or_404(db, session_id)
    from app.storage import build_key, get_storage

    with contextlib.suppress(Exception):
        get_storage().delete_prefix(
            build_key(
                "projects",
                session.project_id,
                "floors",
                session.floor_id,
                "sessions",
                session.id,
            )
        )
    db.delete(session)
    db.commit()
    return Message(message=f"Capture {session_id} deleted")


# ---- upload ------------------------------------------------------------------
@router.post(
    "/captures/{session_id}/upload",
    response_model=list[CaptureFileOut],
    status_code=status.HTTP_201_CREATED,
)
def upload_capture_files(
    session_id: str,
    db: DbSession,
    _: RequireUpload,
    files: Annotated[list[UploadFile], File(description="360 images or videos")],
) -> list[CaptureFileOut]:
    """Upload one or more files to a capture session (§26).

    Files are streamed to the storage provider; nothing is buffered in memory and the request
    thread does no image processing — that happens on the queue (§8).
    """
    session = get_session_or_404(db, session_id)
    if not files:
        raise ValidationError("No files were included in the request")

    stored: list[CaptureFileOut] = []
    for upload in files:
        record = capture_service.store_upload(
            db,
            session,
            filename=upload.filename or "upload",
            stream=upload.file,
            declared_content_type=upload.content_type,
        )
        stored.append(CaptureFileOut.model_validate(record))
    return stored


@router.get("/captures/{session_id}/files", response_model=list[CaptureFileDetail])
def list_capture_files(
    session_id: str, db: DbSession, _: RequireCaptureRead
) -> list[CaptureFileDetail]:
    get_session_or_404(db, session_id)
    files = list(
        db.scalars(
            select(CaptureFile)
            .where(CaptureFile.session_id == session_id)
            .order_by(CaptureFile.created_at)
        )
    )
    details: list[CaptureFileDetail] = []
    for item in files:
        frame_count = int(
            db.scalar(select(func.count(CaptureFrame.id)).where(CaptureFrame.file_id == item.id))
            or 0
        )
        details.append(
            CaptureFileDetail(
                **CaptureFileOut.model_validate(item).model_dump(),
                metadata_json=item.metadata_json,
                frame_count=frame_count,
                asset=sign(item.storage_key),
            )
        )
    return details


@router.delete("/captures/{session_id}/files/{file_id}", response_model=Message)
def delete_capture_file(
    session_id: str, file_id: str, db: DbSession, _: RequireCaptureDelete
) -> Message:
    get_session_or_404(db, session_id)
    record = db.get(CaptureFile, file_id)
    if record is None or record.session_id != session_id:
        raise NotFoundError(f"File {file_id} not found in this capture")
    from app.storage import get_storage

    with contextlib.suppress(Exception):
        get_storage().delete(record.storage_key)
    db.delete(record)
    db.commit()
    return Message(message="File deleted")


# ---- processing --------------------------------------------------------------
@router.post("/captures/{session_id}/process", response_model=ProcessingStatus)
def process_capture(
    session_id: str,
    db: DbSession,
    _: RequireUpload,
    payload: ProcessRequest | None = None,
) -> ProcessingStatus:
    """Queue the processing pipeline. Returns immediately — the request never blocks (§8).

    The body is optional: ``POST /captures/{id}/process`` with no body runs the full pipeline
    with the session's configured frame interval, which is the common case from the UI.
    """
    session = get_session_or_404(db, session_id)
    options = payload or ProcessRequest()
    enqueue_pipeline(
        db,
        session,
        stages=options.stages,
        frame_interval_seconds=options.frame_interval_seconds,
        force=options.force,
    )
    return _processing_status(db, session_id)


@router.get("/captures/{session_id}/status", response_model=ProcessingStatus)
def capture_status(session_id: str, db: DbSession, _: RequireCaptureRead) -> ProcessingStatus:
    get_session_or_404(db, session_id)
    return _processing_status(db, session_id)


@router.post("/captures/{session_id}/retry", response_model=ProcessingStatus)
def retry_capture(session_id: str, db: DbSession, _: RequireUpload) -> ProcessingStatus:
    """Re-run the pipeline from the stage that failed (§34)."""
    session = get_session_or_404(db, session_id)
    if session.status is not CaptureSessionStatus.FAILED:
        raise ConflictError(
            f"This capture is {session.status}, not failed. Use /process with force=true to "
            "reprocess it."
        )
    enqueue_pipeline(db, session, force=True)
    return _processing_status(db, session_id)


def _processing_status(db: DbSession, session_id: str) -> ProcessingStatus:
    session = get_session_or_404(db, session_id)
    progress, jobs = pipeline_progress(db, session_id)
    return ProcessingStatus(
        session_id=session_id,
        status=session.status,
        failure_reason=session.failure_reason,
        progress=progress,
        stages=[ProcessingJobOut.model_validate(job) for job in jobs],
    )


# ---- frames ------------------------------------------------------------------
@router.get("/captures/{session_id}/frames", response_model=Page[CaptureFrameDetail])
def list_capture_frames(
    session_id: str,
    db: DbSession,
    page: Pagination,
    _: RequireCaptureRead,
    verdict: Annotated[QualityVerdict | None, Query()] = None,
    located: Annotated[bool | None, Query(description="Filter by floor-plan placement")] = None,
) -> Page[CaptureFrameDetail]:
    get_session_or_404(db, session_id)
    query = select(CaptureFrame).where(CaptureFrame.session_id == session_id)
    if verdict:
        query = query.join(QualityResult, QualityResult.frame_id == CaptureFrame.id).where(
            QualityResult.verdict == verdict
        )
    if located is True:
        query = query.where(CaptureFrame.location_id.is_not(None))
    elif located is False:
        query = query.where(CaptureFrame.location_id.is_(None))

    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    frames = list(
        db.scalars(
            query.order_by(CaptureFrame.sequence_index).limit(page.limit).offset(page.offset)
        )
    )
    return Page(
        items=[viewer_service.frame_detail(db, frame) for frame in frames],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/frames/{frame_id}", response_model=CaptureFrameDetail)
def get_frame(frame_id: str, db: DbSession, _: RequireCaptureRead) -> CaptureFrameDetail:
    return viewer_service.frame_detail(db, get_frame_or_404(db, frame_id))


@router.get("/frames/{frame_id}/viewer", response_model=ViewerManifest)
def get_viewer_manifest(frame_id: str, db: DbSession, _: RequireCaptureRead) -> ViewerManifest:
    return viewer_service.viewer_manifest(db, get_frame_or_404(db, frame_id))


@router.put("/frames/{frame_id}/location", response_model=CaptureFrameDetail)
def assign_frame_location(
    frame_id: str,
    payload: FrameAssignLocation,
    db: DbSession,
    user: RequireLocationEdit,
) -> CaptureFrameDetail:
    """Place a frame on the floor plan (§12) — the "place capture on floor plan" step.

    Either reference an existing location, or give ``x``/``y`` to create one and bind in a single
    call, which is what the floor-plan editor does when a user drops a new pin on a frame.
    """
    frame = get_frame_or_404(db, frame_id)
    session = get_session_or_404(db, frame.session_id)

    if payload.location_id:
        location = db.get(CaptureLocation, payload.location_id)
        if location is None:
            raise NotFoundError(f"Location {payload.location_id} not found")
    elif payload.x is not None and payload.y is not None:
        floor = db.get(Floor, session.floor_id)
        if floor is None:
            raise NotFoundError("The capture's floor no longer exists")
        location = capture_service.create_location(
            db,
            floor,
            x=payload.x,
            y=payload.y,
            code=payload.code,
            zone_id=session.zone_id,
            created_by_id=user.id,
        )
    else:
        raise ValidationError("Provide either location_id, or both x and y")

    capture_service.assign_frame_to_location(db, frame, location)
    return viewer_service.frame_detail(db, frame)


@router.delete("/frames/{frame_id}/location", response_model=CaptureFrameDetail)
def unassign_frame_location(
    frame_id: str, db: DbSession, _: RequireLocationEdit
) -> CaptureFrameDetail:
    frame = get_frame_or_404(db, frame_id)
    session = get_session_or_404(db, frame.session_id)
    frame.location_id = None
    frame.position_provenance = None
    frame.position_confidence = None
    db.flush()
    from app.processing.stages import build_capture_path

    build_capture_path(db, session)
    db.commit()
    return viewer_service.frame_detail(db, frame)


# ---- capture path ------------------------------------------------------------
@router.get("/captures/{session_id}/path", response_model=CapturePathOut)
def get_capture_path(session_id: str, db: DbSession, _: RequireCaptureRead) -> CapturePathOut:
    get_session_or_404(db, session_id)
    path = db.scalar(select(CapturePath).where(CapturePath.session_id == session_id))
    if path is None:
        raise NotFoundError(
            "No capture path exists yet. Place at least one frame on the floor plan."
        )
    return CapturePathOut.model_validate(path)


@router.get("/floors/{floor_id}/paths", response_model=list[CapturePathOut])
def list_floor_paths(
    floor_id: str,
    db: DbSession,
    _: RequireCaptureRead,
    session_id: Annotated[str | None, Query()] = None,
) -> list[CapturePathOut]:
    """Capture paths on a floor — one polyline per visit, drawn over the plan (§13)."""
    query = select(CapturePath).where(CapturePath.floor_id == floor_id)
    if session_id:
        query = query.where(CapturePath.session_id == session_id)
    rows = db.scalars(query.order_by(CapturePath.created_at.desc()))
    return [CapturePathOut.model_validate(row) for row in rows]


# ---- comparison --------------------------------------------------------------
@router.get("/compare/frames")
def compare_two_frames(
    db: DbSession,
    _: RequireCaptureRead,
    frame_a: Annotated[str, Query(description="First frame id")],
    frame_b: Annotated[str, Query(description="Second frame id")],
) -> dict:
    """Pair two specific frames for the synchronised side-by-side viewer (§17)."""
    return viewer_service.compare_frames(db, frame_a, frame_b)


@router.get("/compare/locations/{location_id}")
def compare_location_dates(
    location_id: str,
    db: DbSession,
    _: RequireCaptureRead,
    date_a: Annotated[str | None, Query(description="ISO date for the first view")] = None,
    date_b: Annotated[str | None, Query(description="ISO date for the second view")] = None,
) -> dict:
    """Compare one location across two dates (§17)."""
    return viewer_service.find_comparison_pair(db, location_id, date_a=date_a, date_b=date_b)


@router.get("/locations/{location_id}/frames", response_model=list[CaptureFrameDetail])
def location_frames(
    location_id: str, db: DbSession, _: RequireCaptureRead
) -> list[CaptureFrameDetail]:
    """Capture history at one location, newest first — the timeline strip in the viewer."""
    if db.get(CaptureLocation, location_id) is None:
        raise NotFoundError(f"Location {location_id} not found")
    frames = list(
        db.scalars(
            select(CaptureFrame)
            .where(CaptureFrame.location_id == location_id)
            .order_by(CaptureFrame.captured_at.desc())
        )
    )
    return [viewer_service.frame_detail(db, frame) for frame in frames]
