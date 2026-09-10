"""Floor plans and capture locations (§12, §13)."""

from __future__ import annotations

import contextlib
import io
import math
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile, status
from PIL import Image
from sqlalchemy import select

from app.api.deps import (
    DbSession,
    RequireCaptureRead,
    RequireFloorPlanEdit,
    RequireLocationEdit,
    RequireProjectRead,
    get_floor_or_404,
    project_id_for_floor,
)
from app.core.config import settings
from app.core.errors import (
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedMediaError,
    ValidationError,
)
from app.core.ids import new_id
from app.core.logging import log_event
from app.models import CaptureFrame, CaptureLocation, FloorPlan
from app.schemas.common import Message
from app.schemas.project import (
    CaptureLocationCreate,
    CaptureLocationOut,
    CaptureLocationUpdate,
    FloorPlanCalibration,
    FloorPlanOut,
)
from app.services import captures as capture_service
from app.services.assets import sign
from app.services.captures import EMPTY_LOCATION_STATS, LocationStats
from app.storage import extension_of, floorplan_key, get_storage

router = APIRouter(tags=["floor plans"])

#: Floor plans are documents, not imagery — 64 MiB is generous for an E1 sheet.
MAX_FLOORPLAN_BYTES = 64 * 1024 * 1024
#: PDFs are accepted and stored, but a raster is needed for pixel coordinates.
PDF_RASTER_NOTE = (
    "PDF floor plans are stored but cannot be used for pixel-coordinate placement until they "
    "are rasterised. Upload a PNG or JPG export of the sheet to place capture points."
)


@router.post(
    "/floors/{floor_id}/floorplan",
    response_model=FloorPlanOut,
    status_code=status.HTTP_201_CREATED,
)
def upload_floor_plan(
    floor_id: str,
    db: DbSession,
    user: RequireFloorPlanEdit,
    file: Annotated[UploadFile, File(description="PNG, JPG or PDF floor plan")],
    name: Annotated[str | None, Form()] = None,
) -> FloorPlanOut:
    # Existence check only: a 404 here is the point, the row itself is not needed.
    get_floor_or_404(db, floor_id)
    project_id = project_id_for_floor(db, floor_id)

    filename = (file.filename or "floorplan").strip()
    extension = extension_of(filename)
    if extension not in settings.allowed_floorplan_extensions:
        raise UnsupportedMediaError(
            f"{extension or 'This file type'} is not a supported floor plan. Allowed: "
            + ", ".join(settings.allowed_floorplan_extensions)
        )

    data = file.file.read(MAX_FLOORPLAN_BYTES + 1)
    if len(data) > MAX_FLOORPLAN_BYTES:
        raise PayloadTooLargeError(
            f"Floor plan exceeds the {MAX_FLOORPLAN_BYTES // (1024 * 1024)} MiB limit"
        )
    if not data:
        raise ValidationError("Floor plan file is empty")

    is_pdf = data[:4] == b"%PDF"
    if is_pdf and extension != ".pdf":
        raise UnsupportedMediaError("File content is a PDF but the extension is not .pdf")

    if is_pdf:
        # Dimensions are unknown without a rasteriser; recorded as 0 and surfaced in the API so
        # the editor can explain the limitation instead of placing points on a zero-size canvas.
        width_px, height_px, content_type = 0, 0, "application/pdf"
    else:
        try:
            with Image.open(io.BytesIO(data)) as image:
                width_px, height_px = image.size
                content_type = Image.MIME.get(image.format or "", "image/png")
        except Exception as exc:
            raise UnsupportedMediaError(
                "Floor plan image could not be read — the file may be corrupt"
            ) from exc

    plan_id = new_id()
    key = floorplan_key(project_id, floor_id, f"{plan_id}{extension}")
    get_storage().put_bytes(key, data, content_type)

    # A newly uploaded plan becomes the active one; earlier revisions are kept for history so an
    # older capture's coordinates remain interpretable against the plan they were placed on.
    for previous in db.scalars(
        select(FloorPlan).where(FloorPlan.floor_id == floor_id, FloorPlan.is_active.is_(True))
    ):
        previous.is_active = False

    plan = FloorPlan(
        id=plan_id,
        floor_id=floor_id,
        name=name or filename,
        storage_key=key,
        content_type=content_type,
        width_px=width_px,
        height_px=height_px,
        file_size_bytes=len(data),
        is_active=True,
        uploaded_by_id=user.id,
        calibration={"note": PDF_RASTER_NOTE} if is_pdf else None,
    )
    db.add(plan)
    db.commit()
    log_event(
        "floorplan.uploaded",
        floor_id=floor_id,
        floor_plan_id=plan.id,
        width_px=width_px,
        height_px=height_px,
    )
    return _plan_out(plan)


@router.get("/floors/{floor_id}/floorplans", response_model=list[FloorPlanOut])
def list_floor_plans(floor_id: str, db: DbSession, _: RequireProjectRead) -> list[FloorPlanOut]:
    get_floor_or_404(db, floor_id)
    rows = db.scalars(
        select(FloorPlan)
        .where(FloorPlan.floor_id == floor_id)
        .order_by(FloorPlan.is_active.desc(), FloorPlan.created_at.desc())
    )
    return [_plan_out(row) for row in rows]


@router.get("/floors/{floor_id}/floorplan", response_model=FloorPlanOut)
def get_active_floor_plan(floor_id: str, db: DbSession, _: RequireProjectRead) -> FloorPlanOut:
    get_floor_or_404(db, floor_id)
    plan = db.scalar(
        select(FloorPlan)
        .where(FloorPlan.floor_id == floor_id, FloorPlan.is_active.is_(True))
        .order_by(FloorPlan.created_at.desc())
        .limit(1)
    )
    if plan is None:
        raise NotFoundError("This floor has no floor plan yet")
    return _plan_out(plan)


@router.post("/floorplans/{plan_id}/activate", response_model=FloorPlanOut)
def activate_floor_plan(plan_id: str, db: DbSession, _: RequireFloorPlanEdit) -> FloorPlanOut:
    plan = db.get(FloorPlan, plan_id)
    if plan is None:
        raise NotFoundError(f"Floor plan {plan_id} not found")
    for other in db.scalars(select(FloorPlan).where(FloorPlan.floor_id == plan.floor_id)):
        other.is_active = other.id == plan.id
    db.commit()
    return _plan_out(plan)


@router.put("/floorplans/{plan_id}/calibration", response_model=FloorPlanOut)
def calibrate_floor_plan(
    plan_id: str,
    payload: FloorPlanCalibration,
    db: DbSession,
    _: RequireFloorPlanEdit,
) -> FloorPlanOut:
    """Set the pixel-to-metre scale from two known points (§12, §30).

    This is the one piece of real-world grounding the platform has, and it is what a future BIM
    alignment will build on — so it is entered by a human against a dimension printed on the
    drawing, and stored as ``manual``. Nothing infers it.
    """
    plan = db.get(FloorPlan, plan_id)
    if plan is None:
        raise NotFoundError(f"Floor plan {plan_id} not found")

    pixel_distance = math.hypot(payload.x2 - payload.x1, payload.y2 - payload.y1)
    if pixel_distance < 1.0:
        raise ValidationError(
            "The two calibration points are too close together to derive a scale. "
            "Pick points that are far apart on the drawing."
        )

    plan.px_per_meter = pixel_distance / payload.real_distance_m
    plan.calibration = {
        "points": [[payload.x1, payload.y1], [payload.x2, payload.y2]],
        "pixel_distance": round(pixel_distance, 3),
        "real_distance_m": payload.real_distance_m,
        "provenance": "manual",
    }
    db.commit()
    return _plan_out(plan)


@router.delete("/floorplans/{plan_id}", response_model=Message)
def delete_floor_plan(plan_id: str, db: DbSession, _: RequireFloorPlanEdit) -> Message:
    plan = db.get(FloorPlan, plan_id)
    if plan is None:
        raise NotFoundError(f"Floor plan {plan_id} not found")
    located = db.scalar(
        select(CaptureLocation.id).where(CaptureLocation.floor_plan_id == plan.id).limit(1)
    )
    if located:
        raise ValidationError(
            "Capture locations reference this floor plan. Deactivate it instead of deleting, so "
            "existing placements keep their reference."
        )
    with contextlib.suppress(Exception):
        get_storage().delete(plan.storage_key)
    db.delete(plan)
    db.commit()
    return Message(message="Floor plan deleted")


def _plan_out(plan: FloorPlan) -> FloorPlanOut:
    return FloorPlanOut(
        **{
            field: getattr(plan, field)
            for field in (
                "id",
                "floor_id",
                "name",
                "content_type",
                "width_px",
                "height_px",
                "file_size_bytes",
                "px_per_meter",
                "calibration",
                "is_active",
                "created_at",
            )
        },
        asset=sign(plan.storage_key),
    )


# ---- capture locations -------------------------------------------------------
@router.post(
    "/floors/{floor_id}/locations",
    response_model=CaptureLocationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_capture_location(
    floor_id: str,
    payload: CaptureLocationCreate,
    db: DbSession,
    user: RequireLocationEdit,
) -> CaptureLocationOut:
    floor = get_floor_or_404(db, floor_id)
    location = capture_service.create_location(
        db,
        floor,
        x=payload.x,
        y=payload.y,
        code=payload.code,
        label=payload.label,
        zone_id=payload.zone_id,
        floor_plan_id=payload.floor_plan_id,
        provenance=payload.provenance,
        confidence=payload.confidence,
        created_by_id=user.id,
    )
    return _location_out(location, EMPTY_LOCATION_STATS)


@router.get("/floors/{floor_id}/locations", response_model=list[CaptureLocationOut])
def list_capture_locations(
    floor_id: str,
    db: DbSession,
    _: RequireCaptureRead,
    zone_id: Annotated[str | None, Query()] = None,
) -> list[CaptureLocationOut]:
    get_floor_or_404(db, floor_id)
    query = select(CaptureLocation).where(CaptureLocation.floor_id == floor_id)
    if zone_id:
        query = query.where(CaptureLocation.zone_id == zone_id)
    rows = list(db.scalars(query.order_by(CaptureLocation.code)))
    stats = capture_service.location_stats(db, floor_id)
    return [_location_out(row, stats.get(row.id, EMPTY_LOCATION_STATS)) for row in rows]


@router.patch("/locations/{location_id}", response_model=CaptureLocationOut)
def update_capture_location(
    location_id: str,
    payload: CaptureLocationUpdate,
    db: DbSession,
    _: RequireLocationEdit,
) -> CaptureLocationOut:
    """Move or relabel a capture point — the drag interaction in the floor-plan editor."""
    location = db.get(CaptureLocation, location_id)
    if location is None:
        raise NotFoundError(f"Location {location_id} not found")

    updates = payload.model_dump(exclude_unset=True)
    if "x" in updates or "y" in updates:
        plan = db.get(FloorPlan, location.floor_plan_id) if location.floor_plan_id else None
        new_x = updates.get("x", location.x)
        new_y = updates.get("y", location.y)
        if (
            plan
            and plan.width_px
            and not (0 <= new_x <= plan.width_px and 0 <= new_y <= plan.height_px)
        ):
            raise ValidationError("Coordinates fall outside the floor plan")
        # Dragging a point is a human decision, so the record now says so regardless of how the
        # point was originally created.
        from app.core.enums import DataProvenance

        location.provenance = DataProvenance.MANUAL
        location.confidence = None

    for field, value in updates.items():
        setattr(location, field, value)
    db.commit()

    _rebuild_paths_for_location(db, location)
    stats = capture_service.location_stats(db, location.floor_id)
    return _location_out(location, stats.get(location.id, EMPTY_LOCATION_STATS))


@router.delete("/locations/{location_id}", response_model=Message)
def delete_capture_location(location_id: str, db: DbSession, _: RequireLocationEdit) -> Message:
    location = db.get(CaptureLocation, location_id)
    if location is None:
        raise NotFoundError(f"Location {location_id} not found")

    # Frames keep existing; they simply become unplaced (ON DELETE SET NULL).
    affected = list(db.scalars(select(CaptureFrame).where(CaptureFrame.location_id == location.id)))
    code = location.code
    db.delete(location)
    db.commit()

    from app.models import CaptureSession
    from app.processing.stages import build_capture_path

    for session_id in {frame.session_id for frame in affected}:
        session = db.get(CaptureSession, session_id)
        if session is not None:
            build_capture_path(db, session)
    db.commit()
    return Message(message=f"Location {code} deleted; {len(affected)} frame(s) unplaced")


def _rebuild_paths_for_location(db: DbSession, location: CaptureLocation) -> None:
    from app.models import CaptureSession
    from app.processing.stages import build_capture_path

    session_ids = set(
        db.scalars(select(CaptureFrame.session_id).where(CaptureFrame.location_id == location.id))
    )
    for session_id in session_ids:
        session = db.get(CaptureSession, session_id)
        if session is not None:
            build_capture_path(db, session)
    if session_ids:
        db.commit()


def _location_out(location: CaptureLocation, stats: LocationStats) -> CaptureLocationOut:
    return CaptureLocationOut(
        **{
            field: getattr(location, field)
            for field in (
                "id",
                "floor_id",
                "zone_id",
                "floor_plan_id",
                "code",
                "label",
                "x",
                "y",
                "provenance",
                "confidence",
                "created_at",
            )
        },
        frame_count=stats.frame_count,
        latest_frame_id=stats.latest_frame_id,
        latest_captured_at=stats.latest_captured_at,
    )
