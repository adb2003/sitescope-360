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
    get_capture_file_or_404,
    get_floor_or_404,
    get_frame_or_404,
    get_location_or_404,
    get_project_or_404,
    get_session_or_404,
)
from app.auth.membership import restrict_to_member_projects
from app.core.enums import (
    CaptureSessionStatus,
    JobType,
    QualityVerdict,
)
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import log_event
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
    AlignTrackRequest,
    AlignTrackResult,
    CaptureFileDetail,
    CaptureFileOut,
    CaptureFrameDetail,
    CapturePathOut,
    CaptureSessionCreate,
    CaptureSessionDetail,
    CaptureSessionOut,
    CaptureSessionUpdate,
    ClearPlacementResult,
    DirectUploadRegistration,
    DirectUploadRequest,
    DirectUploadTicketOut,
    FrameAssignLocation,
    LocalizeMatchOut,
    LocalizeResult,
    MultipartCompleteRequest,
    MultipartResumeRequest,
    MultipartTicketOut,
    MultipartUploadRequest,
    ProcessingJobOut,
    ProcessingStatus,
    ProcessRequest,
    SplitRequest,
    SplitResult,
    UploadPartOut,
    ViewerManifest,
    WalkthroughReportOut,
)
from app.schemas.common import Message, Page
from app.services import alignment as alignment_service
from app.services import captures as capture_service
from app.services import grouping as grouping_service
from app.services import relocalization as relocalization_service
from app.services import viewer as viewer_service
from app.services.assets import sign

router = APIRouter(tags=["captures"])


# ---- sessions ----------------------------------------------------------------
@router.post("/captures", response_model=CaptureSessionOut, status_code=status.HTTP_201_CREATED)
def create_capture_session(
    payload: CaptureSessionCreate, db: DbSession, user: RequireCaptureCreate
) -> CaptureSessionOut:
    # The floor is resolved through the membership-checked resolver *before* the service runs.
    # `create_session` loads the floor itself to walk up to the building and project, which is a
    # plain `db.get` and answers no authorization question — so without this an engineer with
    # `capture:create` could start a capture on any floor id in the deployment, which is a write
    # into another company's job rather than merely a read of it.
    get_floor_or_404(db, payload.floor_id, user)
    session = capture_service.create_session(db, payload, user)
    return CaptureSessionOut.model_validate(session)


@router.get("/captures", response_model=Page[CaptureSessionOut])
def list_capture_sessions(
    db: DbSession,
    page: Pagination,
    user: RequireCaptureRead,
    project_id: Annotated[str | None, Query()] = None,
    floor_id: Annotated[str | None, Query()] = None,
    session_status: Annotated[CaptureSessionStatus | None, Query(alias="status")] = None,
    since: Annotated[
        datetime | None, Query(description="Only sessions started at or after")
    ] = None,
) -> Page[CaptureSessionOut]:
    # Same reasoning as the project list: nothing here resolves one resource, so membership is
    # applied to the query. Without it a filter-free `GET /captures` returns every capture in the
    # deployment, which is the widest leak of the lot — captures carry the imagery.
    query = restrict_to_member_projects(select(CaptureSession), CaptureSession.project_id, db, user)
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
    session_id: str, db: DbSession, user: RequireCaptureRead
) -> CaptureSessionDetail:
    session = get_session_or_404(db, session_id, user)
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

    project = get_project_or_404(db, session.project_id, user)
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
    session_id: str, payload: CaptureSessionUpdate, db: DbSession, user: RequireCaptureCreate
) -> CaptureSessionOut:
    session = get_session_or_404(db, session_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(session, field, value)
    db.commit()
    return CaptureSessionOut.model_validate(session)


@router.post("/captures/{session_id}/finish", response_model=CaptureSessionOut)
def finish_capture_session(
    session_id: str, db: DbSession, user: RequireCaptureCreate
) -> CaptureSessionOut:
    """Close the recording window. Does not start processing — that is an explicit step."""
    session = get_session_or_404(db, session_id, user)
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
    session_id: str, db: DbSession, user: RequireCaptureCreate
) -> CaptureSessionOut:
    session = get_session_or_404(db, session_id, user)
    if session.status is CaptureSessionStatus.READY:
        raise ConflictError("A completed capture cannot be cancelled; delete it instead")
    session.status = CaptureSessionStatus.CANCELLED
    session.ended_at = session.ended_at or datetime.now(UTC)
    db.commit()
    return CaptureSessionOut.model_validate(session)


@router.delete("/captures/{session_id}", response_model=Message)
def delete_capture_session(session_id: str, db: DbSession, user: RequireCaptureDelete) -> Message:
    session = get_session_or_404(db, session_id, user)
    # Not a blind prefix wipe: an upload that was split into several walkthroughs shares its
    # stored frames with them, and this capture may be the one holding the bytes.
    storage = capture_service.delete_session_storage(db, session)
    frames, files = session.frame_count, session.file_count
    db.delete(session)
    db.commit()
    # Logged because it is irreversible and destroys imagery. The counts are the part that
    # cannot be recovered afterwards: once the row is gone, nothing says how much went with it.
    log_event(
        "capture.deleted",
        session_id=session_id,
        project_id=session.project_id,
        floor_id=session.floor_id,
        frames=frames,
        files=files,
        actor_id=user.id,
        swept_prefix=storage.get("swept_prefix"),
    )
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
    user: RequireUpload,
    files: Annotated[list[UploadFile], File(description="360 images or videos")],
) -> list[CaptureFileOut]:
    """Upload one or more files to a capture session (§26).

    Files are streamed to the storage provider; nothing is buffered in memory and the request
    thread does no image processing — that happens on the queue (§8).
    """
    session = get_session_or_404(db, session_id, user)
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


@router.post(
    "/captures/{session_id}/upload-url",
    response_model=DirectUploadTicketOut,
)
def create_direct_upload_url(
    session_id: str,
    payload: DirectUploadRequest,
    db: DbSession,
    user: RequireUpload,
) -> DirectUploadTicketOut:
    """Get a signed URL to PUT one file straight to storage (§7, §24).

    The alternative to `/upload` for large files. That route streams the body through the API,
    which holds a worker for the length of the transfer — fine for a panorama, wasteful for a
    multi-gigabyte walkthrough video. Here the bytes go to object storage directly and the API
    sees only two small requests: this one, and `/files` afterwards.

    Two steps, not one, because the server cannot check bytes it has not seen. Nothing is
    recorded until the upload is registered and the object has been inspected.
    """
    session = get_session_or_404(db, session_id, user)
    ticket = capture_service.prepare_direct_upload(
        db,
        session,
        filename=payload.filename,
        content_type=payload.content_type,
    )
    return DirectUploadTicketOut(
        storage_key=ticket.storage_key,
        upload_url=ticket.upload_url,
        headers=ticket.headers,
        expires_in=ticket.expires_in,
    )


def _ticket_out(ticket: capture_service.MultipartTicket) -> MultipartTicketOut:
    return MultipartTicketOut(
        storage_key=ticket.storage_key,
        upload_id=ticket.upload_id,
        part_size=ticket.part_size,
        part_count=ticket.part_count,
        total_bytes=ticket.total_bytes,
        received=ticket.received,
        parts=[
            UploadPartOut(
                part_number=part.part_number,
                upload_url=part.upload_url,
                headers=part.headers,
                size_bytes=part.size_bytes,
            )
            for part in ticket.parts
        ],
        expires_in=ticket.expires_in,
    )


@router.post("/captures/{session_id}/multipart", response_model=MultipartTicketOut)
def start_multipart_upload(
    session_id: str,
    payload: MultipartUploadRequest,
    db: DbSession,
    user: RequireUpload,
) -> MultipartTicketOut:
    """Begin a chunked upload and get a signed URL for every part (§7, §32).

    The third of three upload routes. `/upload` streams through the API and validates before
    anything is stored; `/upload-url` hands over one presigned PUT; this cuts the file into
    parts so that a connection that drops costs one part rather than the whole transfer — which
    on a site uplink is the difference between a walkthrough that eventually arrives and one
    that never does.
    """
    session = get_session_or_404(db, session_id, user)
    return _ticket_out(
        capture_service.prepare_multipart_upload(
            db,
            session,
            filename=payload.filename,
            size_bytes=payload.size_bytes,
            content_type=payload.content_type,
        )
    )


@router.post("/captures/{session_id}/multipart/resume", response_model=MultipartTicketOut)
def resume_multipart_upload(
    session_id: str,
    payload: MultipartResumeRequest,
    db: DbSession,
    user: RequireUpload,
) -> MultipartTicketOut:
    """Which parts landed, and fresh URLs for the ones that did not.

    A POST rather than a GET despite reading nothing, because the identifiers it needs — a
    storage key and an upload id — are long, and putting them in a query string writes them into
    every access log and proxy cache between here and the client.
    """
    session = get_session_or_404(db, session_id, user)
    return _ticket_out(
        capture_service.resume_multipart_upload(
            session,
            storage_key=payload.storage_key,
            upload_id=payload.upload_id,
            filename=payload.filename,
            size_bytes=payload.size_bytes,
        )
    )


@router.post(
    "/captures/{session_id}/multipart/complete",
    response_model=CaptureFileOut,
    status_code=status.HTTP_201_CREATED,
)
def finish_multipart_upload(
    session_id: str,
    payload: MultipartCompleteRequest,
    db: DbSession,
    user: RequireUpload,
) -> CaptureFileOut:
    """Assemble the parts and take ownership, or refuse and leave them where they are.

    The object does not exist until this returns. An upload missing a part is rejected without
    assembling anything, so the client can resume rather than having to start over — which would
    make the resumable route no better than the one it replaces at the only moment it matters.
    """
    session = get_session_or_404(db, session_id, user)
    record = capture_service.complete_multipart_upload(
        db,
        session,
        storage_key=payload.storage_key,
        upload_id=payload.upload_id,
        filename=payload.filename,
        size_bytes=payload.size_bytes,
    )
    return CaptureFileOut.model_validate(record)


@router.post("/captures/{session_id}/multipart/abort", response_model=Message)
def abort_multipart_upload(
    session_id: str,
    payload: MultipartResumeRequest,
    db: DbSession,
    user: RequireUpload,
) -> Message:
    """Discard an abandoned chunked upload.

    Parts are not objects: they do not show up in a listing, nothing else in this application
    looks for them, and on S3 they are billed until something says otherwise. So giving up needs
    to be an action a client can take, not just something it does by going quiet.
    """
    session = get_session_or_404(db, session_id, user)
    capture_service.abort_multipart_upload(
        session, storage_key=payload.storage_key, upload_id=payload.upload_id
    )
    return Message(message="Upload discarded")


@router.post(
    "/captures/{session_id}/files",
    response_model=CaptureFileOut,
    status_code=status.HTTP_201_CREATED,
)
def register_direct_upload(
    session_id: str,
    payload: DirectUploadRegistration,
    db: DbSession,
    user: RequireUpload,
) -> CaptureFileOut:
    """Register a file uploaded through a signed URL, after checking what actually landed.

    The size cap and the magic-byte sniff run here rather than before storage, because that is
    the only order available once the client wrote the bytes itself. An object that fails either
    is deleted before this returns, so a rejected upload does not stay billable or reachable.

    Safe to call twice: a key already registered returns the existing record rather than a
    second row, which is what a client retrying over a site connection will do.
    """
    session = get_session_or_404(db, session_id, user)
    record = capture_service.register_direct_upload(
        db,
        session,
        storage_key=payload.storage_key,
        filename=payload.filename,
    )
    return CaptureFileOut.model_validate(record)


@router.get("/captures/{session_id}/files", response_model=Page[CaptureFileDetail])
def list_capture_files(
    session_id: str, db: DbSession, page: Pagination, user: RequireCaptureRead
) -> Page[CaptureFileDetail]:
    """The files in one upload, with their metadata and frame counts.

    Paginated because one upload is not one file: a day of stills arrives as several hundred,
    each becoming a row here with its full EXIF payload attached.
    """
    get_session_or_404(db, session_id, user)
    query = select(CaptureFile).where(CaptureFile.session_id == session_id)
    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    files = list(
        db.scalars(query.order_by(CaptureFile.created_at).limit(page.limit).offset(page.offset))
    )

    # One grouped count for the page, not one query per file. At several hundred stills the old
    # shape issued several hundred round trips to render a table nobody had scrolled yet.
    counts: dict[str, int] = {}
    if files:
        rows = db.execute(
            select(CaptureFrame.file_id, func.count(CaptureFrame.id))
            .where(CaptureFrame.file_id.in_([item.id for item in files]))
            .group_by(CaptureFrame.file_id)
        ).all()
        counts = {str(file_id): int(count) for file_id, count in rows}

    return Page(
        items=[
            CaptureFileDetail(
                **CaptureFileOut.model_validate(item).model_dump(),
                metadata_json=item.metadata_json,
                frame_count=counts.get(item.id, 0),
                asset=sign(item.storage_key),
            )
            for item in files
        ],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.delete("/captures/{session_id}/files/{file_id}", response_model=Message)
def delete_capture_file(
    session_id: str, file_id: str, db: DbSession, user: RequireCaptureDelete
) -> Message:
    get_session_or_404(db, session_id, user)
    record = get_capture_file_or_404(db, file_id, user)
    if record is None or record.session_id != session_id:
        raise NotFoundError(f"File {file_id} not found in this capture")
    removed = capture_service.delete_file_storage(db, record)
    filename = record.original_filename
    db.delete(record)
    db.commit()
    log_event(
        "capture.file_deleted",
        session_id=session_id,
        file_id=file_id,
        filename=filename,
        bytes_removed=removed,
        actor_id=user.id,
    )
    return Message(
        message="File deleted"
        if removed
        else "File removed from this capture; its bytes are still used by another walkthrough"
    )


# ---- processing --------------------------------------------------------------
@router.post("/captures/{session_id}/process", response_model=ProcessingStatus)
def process_capture(
    session_id: str,
    db: DbSession,
    user: RequireUpload,
    payload: ProcessRequest | None = None,
) -> ProcessingStatus:
    """Queue the processing pipeline. Returns immediately — the request never blocks (§8).

    The body is optional: ``POST /captures/{id}/process`` with no body runs the full pipeline
    with the session's configured frame interval, which is the common case from the UI.
    """
    session = get_session_or_404(db, session_id, user)
    options = payload or ProcessRequest()
    enqueue_pipeline(
        db,
        session,
        stages=options.stages,
        frame_interval_seconds=options.frame_interval_seconds,
        force=options.force,
    )
    return _processing_status(db, session_id, user)


@router.get("/captures/{session_id}/status", response_model=ProcessingStatus)
def capture_status(session_id: str, db: DbSession, user: RequireCaptureRead) -> ProcessingStatus:
    get_session_or_404(db, session_id, user)
    return _processing_status(db, session_id, user)


@router.post("/captures/{session_id}/retry", response_model=ProcessingStatus)
def retry_capture(session_id: str, db: DbSession, user: RequireUpload) -> ProcessingStatus:
    """Re-run the pipeline from the stage that failed (§34)."""
    session = get_session_or_404(db, session_id, user)
    if session.status is not CaptureSessionStatus.FAILED:
        raise ConflictError(
            f"This capture is {session.status}, not failed. Use /process with force=true to "
            "reprocess it."
        )
    enqueue_pipeline(db, session, force=True)
    return _processing_status(db, session_id, user)


def _processing_status(db: DbSession, session_id: str, user: User) -> ProcessingStatus:
    session = get_session_or_404(db, session_id, user)
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
    user: RequireCaptureRead,
    verdict: Annotated[QualityVerdict | None, Query()] = None,
    located: Annotated[bool | None, Query(description="Filter by floor-plan placement")] = None,
) -> Page[CaptureFrameDetail]:
    get_session_or_404(db, session_id, user)
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
def get_frame(frame_id: str, db: DbSession, user: RequireCaptureRead) -> CaptureFrameDetail:
    return viewer_service.frame_detail(db, get_frame_or_404(db, frame_id, user))


@router.get("/frames/{frame_id}/viewer", response_model=ViewerManifest)
def get_viewer_manifest(frame_id: str, db: DbSession, user: RequireCaptureRead) -> ViewerManifest:
    return viewer_service.viewer_manifest(db, get_frame_or_404(db, frame_id, user))


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
    frame = get_frame_or_404(db, frame_id, user)
    session = get_session_or_404(db, frame.session_id, user)

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
    frame_id: str, db: DbSession, user: RequireLocationEdit
) -> CaptureFrameDetail:
    frame = get_frame_or_404(db, frame_id, user)
    session = get_session_or_404(db, frame.session_id, user)
    frame.location_id = None
    frame.position_provenance = None
    frame.position_confidence = None
    db.flush()
    from app.processing.stages import build_capture_path

    build_capture_path(db, session)
    db.commit()
    return viewer_service.frame_detail(db, frame)


# ---- walkthrough grouping ----------------------------------------------------
@router.get("/captures/{session_id}/segments", response_model=WalkthroughReportOut)
def capture_segments(
    session_id: str, db: DbSession, user: RequireCaptureRead
) -> WalkthroughReportOut:
    """Where this upload looks like it stopped being one walkthrough.

    Read from what MAPPING recorded rather than recomputed: the evidence is the pairwise matching
    that estimated the track, and asking for it again would mean matching every consecutive frame
    a second time to reach the same answer.
    """
    session = get_session_or_404(db, session_id, user)
    report = session.grouping_report
    if not report:
        return WalkthroughReportOut(
            session_id=session.id,
            analysed=False,
            note=(
                "This capture has not been analysed for separate walkthroughs yet. Detection "
                "runs during mapping, so process the capture first."
            ),
        )
    return WalkthroughReportOut(session_id=session.id, analysed=True, **report)


@router.post("/captures/{session_id}/split", response_model=SplitResult)
def split_capture(
    session_id: str, payload: SplitRequest, db: DbSession, user: RequireCaptureCreate
) -> SplitResult:
    """Cut one upload into the walkthroughs the operator confirmed.

    The plan is theirs, not the detector's: they can accept the proposal, move a boundary, or
    split somewhere nothing was proposed. What the platform insists on is that the segments
    tile the capture exactly — every frame in one walkthrough, none in two.
    """
    session = get_session_or_404(db, session_id, user)
    outcome = grouping_service.split_session(
        db,
        session,
        [
            grouping_service.SegmentPlan(
                start_sequence_index=segment.start_sequence_index,
                end_sequence_index=segment.end_sequence_index,
                floor_id=segment.floor_id,
                zone_id=segment.zone_id,
                name=segment.name,
            )
            for segment in payload.segments
        ],
        user,
    )
    db.commit()

    reprocessing: list[str] = []
    if payload.reprocess:
        for capture in [session, *outcome.created]:
            # One capture failing to queue must not strand the rest: the split has already
            # happened and is committed, and a walkthrough nobody mapped is recoverable with the
            # process button. Losing the other walkthroughs' jobs to it would not be.
            with contextlib.suppress(ConflictError, ValidationError):
                enqueue_pipeline(
                    db, capture, stages=[JobType.MAPPING, JobType.VIEWER_DATA], force=True
                )
                reprocessing.append(capture.id)

    return SplitResult(
        parent_id=outcome.parent_id,
        created=[CaptureSessionOut.model_validate(child) for child in outcome.created],
        moved_frames=outcome.moved_frames,
        cleared_track=outcome.cleared_track,
        reprocessing=reprocessing,
        note=outcome.note,
    )


# ---- capture path ------------------------------------------------------------
@router.post("/captures/{session_id}/align", response_model=AlignTrackResult)
def align_capture_to_plan(
    session_id: str,
    payload: AlignTrackRequest,
    db: DbSession,
    user: RequireLocationEdit,
) -> AlignTrackResult:
    """Pin an estimated track to the floor plan from two or more manual anchors (§15).

    The mapping stage recovers the *shape* of the walk and nothing else: monocular estimation
    has no scale, no compass bearing and no origin. The operator supplies all three at once by
    naming two frames and saying where on the plan each was taken; a similarity transform is
    fitted to those and applied to every other frame, so the rest place themselves.

    Refused rather than half-applied when the fit does not hold — too few anchors, anchors that
    are collinear in the estimated track, or a residual too large to believe. A placed track that
    is wrong is worse than an unplaced one, because it looks measured.
    """
    session = get_session_or_404(db, session_id, user)
    result = alignment_service.align_session_to_plan(
        db,
        session,
        [
            alignment_service.Anchor(frame_id=item.frame_id, plan_x=item.x, plan_y=item.y)
            for item in payload.anchors
        ],
        user_id=user.id,
    )
    return AlignTrackResult(
        placed_frames=result.placed_frames,
        scale=result.scale,
        rotation_deg=result.rotation_deg,
        rms_error_px=result.rms_error_px,
        note=result.note,
    )


@router.post("/captures/{session_id}/localize", response_model=LocalizeResult)
def localize_capture_from_earlier_captures(
    session_id: str,
    db: DbSession,
    user: RequireLocationEdit,
    replace_existing: Annotated[
        bool, Query(description="Re-place frames that already have a location")
    ] = False,
) -> LocalizeResult:
    """Place a capture by recognising frames from earlier captures of the same floor (§15).

    The first walk of a floor must be anchored by hand. After that the floor is covered in frames
    whose plan positions are known, so a new walk can be placed by recognising where it stands:
    a frame taken from the same spot as an earlier one differs from it only by rotation, and that
    is measurable. Three such frames place the whole track.

    Runs automatically at the end of processing. This endpoint is for re-running it — after the
    floor's first capture has been anchored, or after more captures have made the floor's
    reference set richer.

    Never raises for the ordinary refusals. "No earlier capture to match against" and "nothing
    recognised" are answers, not errors, and both leave the capture exactly as it was.
    """
    session = get_session_or_404(db, session_id, user)
    outcome = relocalization_service.localize_session_to_plan(
        db, session, user_id=user.id, replace_existing=replace_existing
    )
    db.commit()
    return LocalizeResult(
        ok=outcome.ok,
        mode=outcome.mode,
        reason=outcome.reason,
        note=outcome.note,
        placed_frames=outcome.placed_frames,
        matched_frames=outcome.matched_frames,
        reused_locations=outcome.reused_locations,
        new_locations=outcome.new_locations,
        references_considered=outcome.references_considered,
        generation=outcome.generation,
        rms_error_px=(outcome.transform or {}).get("rms_error_px"),
        matches=[
            LocalizeMatchOut(
                query_index=int(match["query_index"]),
                reference_frame_id=str(match["reference_frame_id"]),
                reference_session_id=str(match["reference_session_id"]),
                location_id=str(match["location_id"]),
                plan_x=float(match["plan_x"]),
                plan_y=float(match["plan_y"]),
                inlier_count=int(match["inlier_count"]),
                parallax_deg=float(match["parallax_deg"]),
                confidence=float(match["confidence"]),
            )
            for match in outcome.report.get("matches", [])
        ],
    )


@router.delete("/captures/{session_id}/placement", response_model=ClearPlacementResult)
def clear_capture_placement(
    session_id: str, db: DbSession, user: RequireLocationEdit
) -> ClearPlacementResult:
    """Undo a capture's placement: unplace its frames and remove the points it invented (§15).

    The counterpart to automatic placement, and the reason automatic placement is safe to run
    unattended. A walk recognised into the wrong corridor is not merely mispositioned — it has
    created points there, and those points are eligible references for the *next* capture, so
    the mistake propagates until something removes them.

    Points the capture only *joined* are left alone: they belong to whoever created them, and
    another capture may be standing on them. So are any an operator declared by hand.
    """
    session = get_session_or_404(db, session_id, user)
    cleared = relocalization_service.clear_session_placement(db, session)
    db.commit()
    return ClearPlacementResult(
        frames_unplaced=cleared.frames_unplaced,
        locations_deleted=cleared.locations_deleted,
        note=cleared.note,
    )


@router.get("/captures/{session_id}/path", response_model=CapturePathOut)
def get_capture_path(session_id: str, db: DbSession, user: RequireCaptureRead) -> CapturePathOut:
    get_session_or_404(db, session_id, user)
    path = db.scalar(select(CapturePath).where(CapturePath.session_id == session_id))
    if path is None:
        raise NotFoundError(
            "No capture path exists yet. Place at least one frame on the floor plan."
        )
    return CapturePathOut.model_validate(path)


@router.get("/floors/{floor_id}/paths", response_model=Page[CapturePathOut])
def list_floor_paths(
    floor_id: str,
    db: DbSession,
    page: Pagination,
    user: RequireCaptureRead,
    session_id: Annotated[str | None, Query()] = None,
) -> Page[CapturePathOut]:
    """Capture paths on a floor — one polyline per visit, drawn over the plan (§13).

    Paginated, and newest first, because this response grows for as long as the floor is in use.
    A path carries its whole ``steps`` array — every frame, at roughly 180 bytes each — so a
    floor walked weekly for two years is megabytes of polyline in one reply, fetched every time
    somebody opens the plan. Newest first means the default page is the paths anybody actually
    wants to see drawn.
    """
    get_floor_or_404(db, floor_id, user)
    query = select(CapturePath).where(CapturePath.floor_id == floor_id)
    if session_id:
        query = query.where(CapturePath.session_id == session_id)
    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = db.scalars(
        query.order_by(CapturePath.created_at.desc()).limit(page.limit).offset(page.offset)
    )
    return Page(
        items=[CapturePathOut.model_validate(row) for row in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


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
    user: RequireCaptureRead,
    date_a: Annotated[str | None, Query(description="ISO date for the first view")] = None,
    date_b: Annotated[str | None, Query(description="ISO date for the second view")] = None,
) -> dict:
    """Compare one location across two dates (§17)."""
    get_location_or_404(db, location_id, user)
    return viewer_service.find_comparison_pair(db, location_id, date_a=date_a, date_b=date_b)


@router.get("/locations/{location_id}/frames", response_model=Page[CaptureFrameDetail])
def location_frames(
    location_id: str, db: DbSession, page: Pagination, user: RequireCaptureRead
) -> Page[CaptureFrameDetail]:
    """Capture history at one location, newest first — the timeline strip in the viewer.

    Paginated because this is the one list in the product that grows forever by design: a
    location accumulates one frame per visit for the life of the job, and comparing dates at a
    place is the feature that makes people revisit it. Newest first, so the default page is the
    recent history the strip opens on.
    """
    get_location_or_404(db, location_id, user)
    query = select(CaptureFrame).where(CaptureFrame.location_id == location_id)
    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    frames = list(
        db.scalars(
            query.order_by(CaptureFrame.captured_at.desc()).limit(page.limit).offset(page.offset)
        )
    )
    return Page(
        items=[viewer_service.frame_detail(db, frame) for frame in frames],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )
