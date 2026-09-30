"""Floor plans and capture locations (§12, §13)."""

from __future__ import annotations

import contextlib
import io
import math
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile, status
from fastapi.responses import Response
from PIL import Image
from sqlalchemy import func, select

from app.api.deps import (
    DbSession,
    RequireCaptureRead,
    RequireFloorPlanEdit,
    RequireLocationEdit,
    RequireProjectRead,
    get_floor_or_404,
    get_location_or_404,
    get_plan_or_404,
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
from app.processing.pdf import looks_like_pdf, render_page
from app.schemas.common import Message
from app.schemas.project import (
    CaptureLocationCreate,
    CaptureLocationOut,
    CaptureLocationUpdate,
    CapturesNearPoint,
    CaptureVisit,
    FloorPlanCalibration,
    FloorPlanOut,
    NearestCaptureLocation,
    SheetChosen,
    SheetOut,
    SheetSetOut,
)
from app.services import captures as capture_service
from app.services import sheets as sheets_service
from app.services.assets import sign
from app.services.captures import EMPTY_LOCATION_STATS, LocationStats
from app.services.spatial import MAX_RESULTS, captures_near, nearest_locations
from app.storage import extension_of, floorplan_key, get_storage

router = APIRouter(tags=["floor plans"])

#: Floor plans are documents, not imagery — 64 MiB is generous for an E1 sheet.
MAX_FLOORPLAN_BYTES = 64 * 1024 * 1024


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
    page: Annotated[
        int,
        Form(
            ge=1,
            description="Which sheet of a PDF to rasterise, 1-based. Ignored for images.",
        ),
    ] = 1,
) -> FloorPlanOut:
    # Existence check only: a 404 here is the point, the row itself is not needed.
    get_floor_or_404(db, floor_id, user)
    project_id = project_id_for_floor(db, floor_id, user)

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

    is_pdf = looks_like_pdf(data)
    if is_pdf and extension != ".pdf":
        raise UnsupportedMediaError("File content is a PDF but the extension is not .pdf")

    render: dict[str, object] | None = None
    source_data = data
    if is_pdf:
        # Rasterised here, in the request, rather than queued. Every positional feature in the
        # application works in plan pixels, so a plan that exists without a raster is a plan the
        # operator can upload and then not use -- which is the state this route used to leave
        # them in. One sheet at 150 DPI is a few hundred milliseconds; waiting for it is better
        # than returning a row that cannot be placed on yet.
        sheet = render_page(data, page=page)
        # The *rendered* bytes become the plan: every consumer downstream -- the viewer, the
        # minimap, the location editor -- reads `storage_key` expecting an image, and pointing it
        # at a PDF is what made this format unusable in the first place. The original is kept
        # beside it, so a future re-render at a different DPI does not need the file re-uploaded.
        data, width_px, height_px, content_type = (
            sheet.png,
            sheet.width_px,
            sheet.height_px,
            "image/png",
        )
        extension = ".png"
        render = sheet.to_metadata()
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

    if is_pdf and render is not None:
        # Keep the sheet it came from. Re-rendering at a different DPI, or pulling a second sheet
        # out of the same set, should not need the operator to find the file again -- and on a
        # site that received the set by email six months ago, they may not be able to.
        source_key = floorplan_key(project_id, floor_id, f"{plan_id}-source.pdf")
        get_storage().put_bytes(source_key, source_data, "application/pdf")
        render["source_storage_key"] = source_key

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
        # `calibration` stays empty: it means the human-entered pixel-to-metre scale, and a PDF
        # says nothing about a building's dimensions however precisely it describes its paper.
        calibration=None,
        render=render,
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
def list_floor_plans(floor_id: str, db: DbSession, user: RequireProjectRead) -> list[FloorPlanOut]:
    get_floor_or_404(db, floor_id, user)
    rows = db.scalars(
        select(FloorPlan)
        .where(FloorPlan.floor_id == floor_id)
        .order_by(FloorPlan.is_active.desc(), FloorPlan.created_at.desc())
    )
    return [_plan_out(row) for row in rows]


@router.get("/floors/{floor_id}/floorplan", response_model=FloorPlanOut)
def get_active_floor_plan(floor_id: str, db: DbSession, user: RequireProjectRead) -> FloorPlanOut:
    get_floor_or_404(db, floor_id, user)
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
def activate_floor_plan(plan_id: str, db: DbSession, user: RequireFloorPlanEdit) -> FloorPlanOut:
    plan = get_plan_or_404(db, plan_id, user)
    for other in db.scalars(select(FloorPlan).where(FloorPlan.floor_id == plan.floor_id)):
        other.is_active = other.id == plan.id
    db.commit()
    return _plan_out(plan)


@router.put("/floorplans/{plan_id}/calibration", response_model=FloorPlanOut)
def calibrate_floor_plan(
    plan_id: str,
    payload: FloorPlanCalibration,
    db: DbSession,
    user: RequireFloorPlanEdit,
) -> FloorPlanOut:
    """Set the pixel-to-metre scale from two known points (§12, §30).

    This is the one piece of real-world grounding the platform has, and it is what a future BIM
    alignment will build on — so it is entered by a human against a dimension printed on the
    drawing, and stored as ``manual``. Nothing infers it.
    """
    plan = get_plan_or_404(db, plan_id, user)

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
def delete_floor_plan(plan_id: str, db: DbSession, user: RequireFloorPlanEdit) -> Message:
    plan = get_plan_or_404(db, plan_id, user)
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
    # And the PDF it was rendered from, which is a second object under a key only the plan row
    # knows. Deleting the row without it leaves the sheet in storage with nothing referencing it.
    #
    # Unless another revision is still rendering from that same PDF. Choosing a different sheet
    # of a drawing set creates a revision that *shares* `source_storage_key` rather than copying
    # the file, so deleting one revision would take the set out from under its siblings — they
    # would keep displaying their own rasters and quietly lose the ability to switch sheet.
    source_key = (plan.render or {}).get("source_storage_key")
    if isinstance(source_key, str) and not _source_shared_with_another_plan(db, plan, source_key):
        with contextlib.suppress(Exception):
            get_storage().delete(source_key)
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
                "render",
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
    floor = get_floor_or_404(db, floor_id, user)
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
    user: RequireCaptureRead,
    zone_id: Annotated[str | None, Query()] = None,
) -> list[CaptureLocationOut]:
    get_floor_or_404(db, floor_id, user)
    query = select(CaptureLocation).where(CaptureLocation.floor_id == floor_id)
    if zone_id:
        query = query.where(CaptureLocation.zone_id == zone_id)
    rows = list(db.scalars(query.order_by(CaptureLocation.code)))
    stats = capture_service.location_stats(db, floor_id)
    return [_location_out(row, stats.get(row.id, EMPTY_LOCATION_STATS)) for row in rows]


@router.get(
    "/floors/{floor_id}/locations/nearest",
    response_model=list[NearestCaptureLocation],
)
def find_nearest_capture_locations(
    floor_id: str,
    db: DbSession,
    user: RequireCaptureRead,
    x: Annotated[float, Query(description="Floor-plan x in plan pixels")],
    y: Annotated[float, Query(description="Floor-plan y in plan pixels")],
    limit: Annotated[int, Query(ge=1, le=MAX_RESULTS)] = 1,
    within_px: Annotated[float | None, Query(gt=0)] = None,
) -> list[NearestCaptureLocation]:
    """Which capture points are closest to a point on the plan (§13).

    The viewer's minimap answers this in the browser from points it has already loaded, which is
    the right call for a click: no round trip. This endpoint is for the callers that have a
    coordinate and no loaded plan — a deep link into a floor, the compare view resolving one
    date's click against another date's points, or any client that should not have to download
    every location to find one.
    """
    get_floor_or_404(db, floor_id, user)
    found = nearest_locations(db, floor_id, x, y, limit=limit, within_px=within_px)
    if not found:
        return []

    stats = capture_service.location_stats(db, floor_id)
    return [
        NearestCaptureLocation(
            location=_location_out(location, stats.get(location.id, EMPTY_LOCATION_STATS)),
            distance_px=round(distance, 3),
        )
        for location, distance in found
    ]


@router.get(
    "/floors/{floor_id}/captures-near",
    response_model=list[CapturesNearPoint],
)
def find_captures_near_point(
    floor_id: str,
    db: DbSession,
    user: RequireCaptureRead,
    x: Annotated[float, Query(description="Floor-plan x in plan pixels")],
    y: Annotated[float, Query(description="Floor-plan y in plan pixels")],
    within_px: Annotated[float, Query(gt=0, description="Search radius in plan pixels")] = 60.0,
    max_points: Annotated[int, Query(ge=1, le=10)] = 3,
) -> list[CapturesNearPoint]:
    """What has been captured near a point on the plan, across every date (§17).

    The time-travel question, asked by coordinate rather than by location id — because the click
    that asks it lands on the drawing, not on a capture point. The radius is what turns one into
    the other, and an empty list is a real answer: nobody walked there.

    Distinct from `/locations/nearest`, which answers "which point is this" and says nothing
    about when it was visited. Both exist because the viewer needs each separately: the minimap
    resolves a click instantly from points it has already loaded, and this fills the history
    panel beside it.
    """
    get_floor_or_404(db, floor_id, user)
    found = captures_near(db, floor_id, x, y, within_px=within_px, max_points=max_points)
    if not found:
        return []

    stats = capture_service.location_stats(db, floor_id)
    return [
        CapturesNearPoint(
            location=_location_out(location, stats.get(location.id, EMPTY_LOCATION_STATS)),
            distance_px=round(distance, 3),
            visits=[
                CaptureVisit(
                    frame_id=frame.id,
                    session_id=frame.session_id,
                    captured_at=frame.captured_at,
                    thumbnail=sign(frame.thumbnail_key),
                    full=sign(frame.storage_key),
                )
                for frame in frames
            ],
        )
        for location, distance, frames in found
    ]


@router.patch("/locations/{location_id}", response_model=CaptureLocationOut)
def update_capture_location(
    location_id: str,
    payload: CaptureLocationUpdate,
    db: DbSession,
    user: RequireLocationEdit,
) -> CaptureLocationOut:
    """Move or relabel a capture point — the drag interaction in the floor-plan editor."""
    location = get_location_or_404(db, location_id, user)

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
def delete_capture_location(location_id: str, db: DbSession, user: RequireLocationEdit) -> Message:
    location = get_location_or_404(db, location_id, user)

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
                "placement_generation",
                "created_at",
            )
        },
        frame_count=stats.frame_count,
        latest_frame_id=stats.latest_frame_id,
        latest_captured_at=stats.latest_captured_at,
    )


# ---- sheet sets --------------------------------------------------------------
@router.get("/floorplans/{plan_id}/sheets", response_model=SheetSetOut)
def list_plan_sheets(plan_id: str, db: DbSession, user: RequireProjectRead) -> SheetSetOut:
    """What is in the drawing set this plan came from (§22).

    Nothing is rendered here — paper sizes come straight off the page objects, so a set with
    hundreds of sheets answers immediately and the picker fetches previews only for the sheets
    somebody actually looks at.
    """
    plan = get_plan_or_404(db, plan_id, user)
    sheets = sheets_service.list_sheets(plan)
    render = plan.render or {}
    current = render.get("page")
    return SheetSetOut(
        floor_plan_id=plan.id,
        page_count=len(sheets),
        current_page=int(current) if current else None,
        sheets=[
            SheetOut(
                page=sheet.page,
                width_pt=round(sheet.width_pt, 1),
                height_pt=round(sheet.height_pt, 1),
                orientation=sheet.orientation,
            )
            for sheet in sheets
        ],
    )


@router.get(
    "/floorplans/{plan_id}/sheets/{page}/preview",
    responses={200: {"content": {"image/png": {}}, "description": "A small PNG of that sheet"}},
)
def preview_plan_sheet(
    plan_id: str,
    page: int,
    db: DbSession,
    user: RequireProjectRead,
) -> Response:
    """A thumbnail of one sheet, so the operator can choose by looking rather than guessing.

    Returned as image bytes rather than a signed URL: the preview is rendered on demand from the
    stored PDF and never written to storage, because caching forty thumbnails per set to save a
    few milliseconds each would be more to keep consistent than it is worth.
    """
    plan = get_plan_or_404(db, plan_id, user)
    png = sheets_service.preview(plan, page)
    return Response(
        content=png,
        media_type="image/png",
        # Immutable for a day: the bytes for (plan, page) cannot change, since the source PDF a
        # plan points at is never rewritten.
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/floorplans/{plan_id}/sheets/{page}", response_model=SheetChosen)
def choose_plan_sheet(
    plan_id: str, page: int, db: DbSession, user: RequireFloorPlanEdit
) -> SheetChosen:
    """Use a different sheet of the same set as this floor's plan (§22).

    Creates a **new revision** rather than re-rendering in place. Capture locations are stored in
    the pixels of the plan they were placed on, and another sheet is a different drawing at a
    different scale — overwriting the raster underneath them would move every placed point to a
    coordinate that means nothing, with nothing in the data recording that it had happened. The
    previous revision is kept and deactivated, exactly as a re-upload would leave it.
    """
    plan = get_plan_or_404(db, plan_id, user)
    revision = sheets_service.choose_sheet(db, plan, page, user)

    # Points are not moved — that is the whole reason this is a revision — but the floor-plan
    # editor draws every point on the floor over whichever plan is active, so any placed on an
    # earlier sheet will now appear on this one at coordinates that mean nothing here. Counted
    # and returned rather than left to be discovered: only the operator can decide whether to
    # re-place them or go back to the sheet they were placed on.
    stranded = int(
        db.scalar(
            select(func.count(CaptureLocation.id)).where(
                CaptureLocation.floor_id == revision.floor_id,
                CaptureLocation.floor_plan_id.is_not(None),
                CaptureLocation.floor_plan_id != revision.id,
            )
        )
        or 0
    )
    db.commit()
    return SheetChosen(
        plan=_plan_out(revision),
        points_from_earlier_sheets=stranded,
        note=(
            f"{stranded} capture point(s) were placed on an earlier sheet of this set. They have "
            "not been moved, and their positions describe that sheet rather than this one — "
            "re-place them here, or switch back to the sheet they belong to."
        )
        if stranded
        else "No capture points had been placed on an earlier sheet of this set.",
    )


def _source_shared_with_another_plan(db: DbSession, plan: FloorPlan, source_key: str) -> bool:
    """Is some other revision of this floor still rendering from that same drawing set?

    Cheap and exact enough: a floor has a handful of plan revisions, and the key lives inside a
    JSON column, so this reads them rather than asking the database to index into JSON on two
    dialects. What it must never do is guess — a wrong `False` deletes a set that is still in use.
    """
    siblings = db.scalars(
        select(FloorPlan).where(FloorPlan.floor_id == plan.floor_id).where(FloorPlan.id != plan.id)
    )
    return any(
        (sibling.render or {}).get("source_storage_key") == source_key for sibling in siblings
    )
