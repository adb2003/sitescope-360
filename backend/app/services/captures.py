"""Capture-session domain logic: creation, upload, frame/location binding, viewer data."""

from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import BinaryIO, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    CaptureSessionStatus,
    DataProvenance,
    FileKind,
    ProjectionType,
)
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedMediaError,
    ValidationError,
)
from app.core.ids import location_code, new_capture_session_id, new_id
from app.core.logging import log_event
from app.models import (
    Building,
    CaptureFile,
    CaptureFrame,
    CaptureLocation,
    CaptureSession,
    Floor,
    FloorPlan,
    User,
    Zone,
)
from app.processing.media import sniff
from app.schemas.capture import CaptureSessionCreate
from app.storage import extension_of, get_storage, session_key


def create_session(db: Session, payload: CaptureSessionCreate, operator: User) -> CaptureSession:
    """Create a capture session, resolving the project/building chain from the floor (§33)."""
    floor = db.get(Floor, payload.floor_id)
    if floor is None:
        raise NotFoundError(f"Floor {payload.floor_id} not found")
    building = db.get(Building, floor.building_id)
    if building is None:
        raise NotFoundError("The floor's building no longer exists")

    if payload.zone_id:
        zone = db.get(Zone, payload.zone_id)
        if zone is None or zone.floor_id != floor.id:
            raise ValidationError("That zone does not belong to the selected floor")

    started_at = payload.started_at or datetime.now(UTC)

    # Idempotent create for offline mobile clients (§32): a retried request that carries the id
    # the phone already generated returns the existing session instead of a duplicate.
    session_id = payload.client_session_id or new_capture_session_id(started_at)
    existing = db.get(CaptureSession, session_id)
    if existing is not None:
        if existing.floor_id != floor.id:
            raise ConflictError(
                f"Capture session id {session_id} already exists on a different floor"
            )
        return existing

    session = CaptureSession(
        id=session_id,
        project_id=building.project_id,
        building_id=building.id,
        floor_id=floor.id,
        zone_id=payload.zone_id,
        name=payload.name,
        status=CaptureSessionStatus.DRAFT,
        started_at=started_at,
        operator_id=operator.id,
        capture_source=payload.capture_source,
        camera_device_id=payload.camera_device_id,
        camera_label=payload.camera_label,
        frame_interval_seconds=payload.frame_interval_seconds,
        notes=payload.notes,
        gps_latitude=payload.gps_latitude,
        gps_longitude=payload.gps_longitude,
    )
    db.add(session)
    db.commit()
    log_event(
        "capture.created",
        session_id=session.id,
        project_id=session.project_id,
        floor_id=session.floor_id,
        operator_id=operator.id,
        capture_source=str(session.capture_source),
    )
    return session


def store_upload(
    db: Session,
    session: CaptureSession,
    *,
    filename: str,
    stream: BinaryIO,
    declared_content_type: str | None = None,
) -> CaptureFile:
    """Validate and persist one uploaded file (§7, §44).

    Order matters. The extension allow-list and the magic-byte check both run *before* anything
    is written, and the size cap is enforced *while* streaming rather than by trusting a
    Content-Length header — so an oversized or disguised upload never reaches storage.
    """
    if session.status in (CaptureSessionStatus.CANCELLED,):
        raise ConflictError("This capture session was cancelled")

    safe_name = PurePosixPath(filename).name
    if not safe_name:
        raise ValidationError("Upload is missing a filename")

    extension = extension_of(safe_name)
    if extension not in settings.allowed_capture_extensions:
        raise UnsupportedMediaError(
            f"{extension or 'This file type'} is not supported. Allowed: "
            + ", ".join(settings.allowed_capture_extensions)
        )

    header = stream.read(64)
    stream.seek(0)
    sniffed = sniff(header, safe_name)
    if sniffed.kind is FileKind.UNKNOWN:
        raise UnsupportedMediaError(f"{safe_name} is not a supported image or video")

    file_id = new_id()
    key = session_key(
        session.project_id,
        session.floor_id,
        session.id,
        "source",
        f"{file_id}{extension}",
    )

    digest = hashlib.sha256()
    total = 0
    limit = settings.max_upload_bytes

    # _CountingStream forwards every other attribute to the wrapped upload, so it satisfies the
    # provider contract without inheriting from BinaryIO (which is a Protocol-shaped ABC that
    # cannot be usefully subclassed here).
    counting = cast("BinaryIO", _CountingStream(stream, digest, limit))
    storage = get_storage()
    try:
        stored = storage.put(key, counting, sniffed.content_type)
    except PayloadTooLargeError:
        # Remove the partial object so a rejected upload leaves nothing behind.
        with contextlib.suppress(Exception):
            storage.delete(key)
        raise
    total = stored.size_bytes
    checksum = digest.hexdigest()

    duplicate = db.scalar(
        select(CaptureFile).where(
            CaptureFile.session_id == session.id,
            CaptureFile.checksum_sha256 == checksum,
        )
    )
    if duplicate is not None:
        # A retried mobile upload of the same bytes: keep the original record, drop the new copy.
        with contextlib.suppress(Exception):
            storage.delete(key)
        return duplicate

    capture_file = CaptureFile(
        id=file_id,
        session_id=session.id,
        original_filename=safe_name,
        storage_key=key,
        content_type=sniffed.content_type,
        file_size_bytes=total,
        checksum_sha256=checksum,
        kind=sniffed.kind,
        # The VALIDATE stage decides the projection by inspecting pixels; at upload time all we
        # honestly know is the container format.
        projection=ProjectionType.UNKNOWN,
    )
    db.add(capture_file)
    session.status = CaptureSessionStatus.UPLOADED
    db.flush()
    session.file_count = int(
        db.scalar(select(func.count(CaptureFile.id)).where(CaptureFile.session_id == session.id))
        or 0
    )
    db.commit()

    log_event(
        "file.uploaded",
        session_id=session.id,
        file_id=capture_file.id,
        filename=safe_name,
        size_bytes=total,
        content_type=sniffed.content_type,
    )
    return capture_file


class _CountingStream:
    """Wraps an upload stream to hash it and abort past the size cap."""

    def __init__(self, inner: BinaryIO, digest: object, limit: int) -> None:
        self._inner = inner
        self._digest = digest
        self._limit = limit
        self._total = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._inner.read(size)
        if chunk:
            self._total += len(chunk)
            if self._total > self._limit:
                raise PayloadTooLargeError(
                    f"Upload exceeds the {self._limit / (1024**3):.1f} GiB limit"
                )
            self._digest.update(chunk)  # type: ignore[attr-defined]
        return chunk

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


# ---- location binding --------------------------------------------------------
def next_location_code(db: Session, floor_id: str) -> str:
    """``P01``, ``P02``, … the first code not already used on this floor.

    Reuses gaps left by deletions so codes stay short and dense, which matters because operators
    read them aloud.
    """
    used = set(db.scalars(select(CaptureLocation.code).where(CaptureLocation.floor_id == floor_id)))
    for index in range(1, 1000):
        candidate = location_code(index)
        if candidate not in used:
            return candidate
    raise ValidationError("This floor already has 999 capture locations")


def create_location(
    db: Session,
    floor: Floor,
    *,
    x: float,
    y: float,
    code: str | None = None,
    label: str | None = None,
    zone_id: str | None = None,
    floor_plan_id: str | None = None,
    provenance: DataProvenance = DataProvenance.MANUAL,
    confidence: float | None = None,
    created_by_id: str | None = None,
) -> CaptureLocation:
    plan = _resolve_plan(db, floor, floor_plan_id)
    # Coordinates are floor-plan pixels; a point outside the raster is a client bug, and
    # silently clamping it would hide a broken editor.
    if plan is not None and not (0 <= x <= plan.width_px and 0 <= y <= plan.height_px):
        raise ValidationError(
            "Coordinates fall outside the floor plan",
            floor_plan_size=[plan.width_px, plan.height_px],
            given=[x, y],
        )

    if zone_id:
        zone = db.get(Zone, zone_id)
        if zone is None or zone.floor_id != floor.id:
            raise ValidationError("That zone does not belong to this floor")

    resolved_code = code or next_location_code(db, floor.id)
    if db.scalar(
        select(CaptureLocation.id).where(
            CaptureLocation.floor_id == floor.id, CaptureLocation.code == resolved_code
        )
    ):
        raise ConflictError(f"Location {resolved_code} already exists on this floor")

    location = CaptureLocation(
        floor_id=floor.id,
        zone_id=zone_id,
        floor_plan_id=plan.id if plan else None,
        code=resolved_code,
        label=label,
        x=x,
        y=y,
        provenance=provenance,
        confidence=confidence,
        created_by_id=created_by_id,
    )
    db.add(location)
    db.commit()
    return location


def _resolve_plan(db: Session, floor: Floor, floor_plan_id: str | None) -> FloorPlan | None:
    if floor_plan_id:
        plan = db.get(FloorPlan, floor_plan_id)
        if plan is None or plan.floor_id != floor.id:
            raise ValidationError("That floor plan does not belong to this floor")
        return plan
    return db.scalar(
        select(FloorPlan)
        .where(FloorPlan.floor_id == floor.id, FloorPlan.is_active.is_(True))
        .order_by(FloorPlan.created_at.desc())
        .limit(1)
    )


def assign_frame_to_location(
    db: Session, frame: CaptureFrame, location: CaptureLocation
) -> CaptureFrame:
    """Bind a frame to a floor-plan location and rebuild the session's path (§12, §13)."""
    session = db.get(CaptureSession, frame.session_id)
    if session is None:
        raise NotFoundError("The frame's capture session no longer exists")
    if location.floor_id != session.floor_id:
        raise ValidationError(
            "A frame can only be placed on the floor its capture session belongs to"
        )

    frame.location_id = location.id
    frame.position_provenance = DataProvenance.MANUAL
    frame.position_confidence = 1.0
    db.flush()

    from app.processing.stages import build_capture_path

    build_capture_path(db, session)
    db.commit()
    log_event(
        "capture.frame_located",
        session_id=session.id,
        frame_id=frame.id,
        location_id=location.id,
        code=location.code,
    )
    return frame


@dataclass(frozen=True, slots=True)
class LocationStats:
    """Per-location capture roll-up.

    A typed value rather than a ``dict[str, object]``: the callers feed these straight into a
    response model, and an untyped dict forced every field through a cast or a type-ignore at
    the point where correctness actually mattered.
    """

    frame_count: int = 0
    latest_frame_id: str | None = None
    latest_captured_at: datetime | None = None


EMPTY_LOCATION_STATS = LocationStats()


def location_stats(db: Session, floor_id: str) -> dict[str, LocationStats]:
    """``{location_id: LocationStats}`` for every visited location on a floor.

    Two grouped queries for the whole floor rather than per-point lookups, so rendering 60
    capture points on a plan does not become 120 round trips (§35).
    """
    rows = db.execute(
        select(
            CaptureFrame.location_id,
            func.count(CaptureFrame.id),
            func.max(CaptureFrame.captured_at),
        )
        .join(CaptureLocation, CaptureLocation.id == CaptureFrame.location_id)
        .where(CaptureLocation.floor_id == floor_id)
        .group_by(CaptureFrame.location_id)
    ).all()
    if not rows:
        return {}

    # Resolve the newest frame per location in one pass instead of a query per location.
    newest: dict[str, str] = {}
    latest_rows = db.execute(
        select(CaptureFrame.location_id, CaptureFrame.id, CaptureFrame.captured_at)
        .join(CaptureLocation, CaptureLocation.id == CaptureFrame.location_id)
        .where(CaptureLocation.floor_id == floor_id)
        .order_by(CaptureFrame.location_id, CaptureFrame.captured_at.desc())
    ).all()
    for location_id, frame_id, _captured_at in latest_rows:
        newest.setdefault(location_id, frame_id)

    return {
        str(location_id): LocationStats(
            frame_count=int(count),
            latest_frame_id=newest.get(location_id),
            latest_captured_at=latest,
        )
        for location_id, count, latest in rows
    }
