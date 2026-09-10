"""Viewer manifests, frame detail, and date comparison (§11, §17)."""

from __future__ import annotations

from datetime import UTC

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import log_event
from app.models import (
    Annotation,
    Building,
    CaptureFrame,
    CaptureLocation,
    CaptureSession,
    Floor,
    Project,
    QualityResult,
    Zone,
)
from app.schemas.capture import (
    CaptureFrameDetail,
    CaptureFrameOut,
    FrameHistoryEntry,
    QualityOut,
    ViewerManifest,
    ViewerNeighbour,
)
from app.services.assets import sign, sign_url

#: How many nearby frames to offer as walk targets in the viewer.
MAX_NEIGHBOURS = 6
MAX_HISTORY = 24


def frame_detail(db: Session, frame: CaptureFrame) -> CaptureFrameDetail:
    context = _context(db, frame)
    quality = db.scalar(select(QualityResult).where(QualityResult.frame_id == frame.id))
    annotation_count = int(
        db.scalar(select(func.count(Annotation.id)).where(Annotation.frame_id == frame.id)) or 0
    )

    base = CaptureFrameOut.model_validate(frame).model_dump()
    base["quality"] = QualityOut.model_validate(quality) if quality else None

    return CaptureFrameDetail(
        **base,
        location_code=context.get("location_code"),
        floor_id=context.get("floor_id"),
        floor_name=context.get("floor_name"),
        project_id=context.get("project_id"),
        project_name=context.get("project_name"),
        zone_name=context.get("zone_name"),
        thumbnail=sign(frame.thumbnail_key),
        preview=sign(frame.preview_key),
        full=sign(frame.storage_key),
        annotation_count=annotation_count,
    )


def viewer_manifest(db: Session, frame: CaptureFrame) -> ViewerManifest:
    """Everything the 360 viewer needs for one frame, in a single request (§11, §35)."""
    detail = frame_detail(db, frame)
    siblings = list(
        db.scalars(
            select(CaptureFrame)
            .where(CaptureFrame.session_id == frame.session_id)
            .order_by(CaptureFrame.sequence_index)
        )
    )
    index = next((i for i, item in enumerate(siblings) if item.id == frame.id), None)

    previous_id = siblings[index - 1].id if index is not None and index > 0 else None
    next_id = siblings[index + 1].id if index is not None and index + 1 < len(siblings) else None

    log_event("viewer.opened", frame_id=frame.id, session_id=frame.session_id)
    return ViewerManifest(
        frame=detail,
        neighbours=_neighbours(frame, siblings),
        previous_frame_id=previous_id,
        next_frame_id=next_id,
        history=frame_history(db, frame),
    )


def _neighbours(frame: CaptureFrame, siblings: list[CaptureFrame]) -> list[ViewerNeighbour]:
    """Nearest frames by estimated relative position, else by sequence order.

    Sorting by geometry rather than by capture order is what makes the viewer feel like walking a
    building instead of scrubbing a video: the frame across the corridor is one click away even
    though it was shot ten minutes later.
    """
    if frame.rel_x is None or frame.rel_y is None:
        near = [item for item in siblings if item.id != frame.id][:MAX_NEIGHBOURS]
        return [
            ViewerNeighbour(
                frame_id=item.id,
                location_code=item.location.code if item.location else None,
                direction_deg=None,
                distance=None,
            )
            for item in near
        ]

    scored: list[tuple[float, float, CaptureFrame]] = []
    for item in siblings:
        if item.id == frame.id or item.rel_x is None or item.rel_y is None:
            continue
        dx = item.rel_x - frame.rel_x
        dy = item.rel_y - frame.rel_y
        distance = float(np.hypot(dx, dy))
        bearing = float(np.degrees(np.arctan2(dx, dy))) % 360.0
        scored.append((distance, bearing, item))

    scored.sort(key=lambda entry: entry[0])
    return [
        ViewerNeighbour(
            frame_id=item.id,
            location_code=item.location.code if item.location else None,
            direction_deg=round(bearing, 1),
            distance=round(distance, 4),
        )
        for distance, bearing, item in scored[:MAX_NEIGHBOURS]
    ]


def frame_history(db: Session, frame: CaptureFrame) -> list[FrameHistoryEntry]:
    """Frames at the same location on other dates — the basis of compare and history (§17)."""
    if not frame.location_id:
        return []
    rows = db.execute(
        select(CaptureFrame, QualityResult.verdict)
        .outerjoin(QualityResult, QualityResult.frame_id == CaptureFrame.id)
        .where(CaptureFrame.location_id == frame.location_id)
        .order_by(CaptureFrame.captured_at.desc())
        .limit(MAX_HISTORY)
    ).all()
    return [
        FrameHistoryEntry(
            frame_id=item.id,
            session_id=item.session_id,
            captured_at=item.captured_at,
            quality_verdict=verdict,
            thumbnail_url=sign_url(item.thumbnail_key),
        )
        for item, verdict in rows
    ]


def compare_frames(db: Session, frame_a_id: str, frame_b_id: str) -> dict[str, object]:
    """Pair two frames for the side-by-side / slider comparison (§17)."""
    frame_a = db.get(CaptureFrame, frame_a_id)
    frame_b = db.get(CaptureFrame, frame_b_id)
    if frame_a is None or frame_b is None:
        raise NotFoundError("One or both frames could not be found")

    same_location = frame_a.location_id is not None and frame_a.location_id == frame_b.location_id
    note = None
    if not same_location:
        note = (
            "These frames are not bound to the same capture location, so the two views are not "
            "guaranteed to show the same spot. Assign both to a location for a true comparison."
        )

    return {
        "a": frame_detail(db, frame_a),
        "b": frame_detail(db, frame_b),
        "same_location": same_location,
        "days_apart": abs((frame_b.captured_at - frame_a.captured_at).days),
        "note": note,
    }


def find_comparison_pair(
    db: Session, location_id: str, *, date_a: str | None = None, date_b: str | None = None
) -> dict[str, object]:
    """Pick the frames closest to two requested dates at one location.

    "Closest to" rather than "exactly on" is deliberate: nobody captures a site on the exact
    calendar day a report asks about, and failing the request would be less useful than
    answering with the nearest visit and saying which one it used.
    """
    location = db.get(CaptureLocation, location_id)
    if location is None:
        raise NotFoundError(f"Location {location_id} not found")

    frames = list(
        db.scalars(
            select(CaptureFrame)
            .where(CaptureFrame.location_id == location_id)
            .order_by(CaptureFrame.captured_at)
        )
    )
    if len(frames) < 2:
        raise ValidationError(
            f"Location {location.code} has only {len(frames)} capture(s). "
            "At least two visits are needed to compare dates.",
            available=len(frames),
        )

    chosen_a = _closest(frames, date_a) or frames[0]
    chosen_b = _closest(frames, date_b) or frames[-1]
    if chosen_a.id == chosen_b.id:
        chosen_a, chosen_b = frames[0], frames[-1]

    return compare_frames(db, chosen_a.id, chosen_b.id)


def _closest(frames: list[CaptureFrame], iso_date: str | None) -> CaptureFrame | None:
    if not iso_date:
        return None
    from datetime import datetime

    try:
        target = datetime.fromisoformat(iso_date)
    except ValueError as exc:
        raise ValidationError(f"{iso_date!r} is not a valid ISO date") from exc
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return min(frames, key=lambda frame: abs((frame.captured_at - target).total_seconds()))


def _context(db: Session, frame: CaptureFrame) -> dict[str, str | None]:
    row = db.execute(
        select(Floor.id, Floor.name, Project.id, Project.name, Zone.name)
        .select_from(CaptureSession)
        .join(Floor, Floor.id == CaptureSession.floor_id)
        .join(Building, Building.id == Floor.building_id)
        .join(Project, Project.id == Building.project_id)
        .outerjoin(Zone, Zone.id == CaptureSession.zone_id)
        .where(CaptureSession.id == frame.session_id)
    ).first()
    if row is None:
        return {}
    floor_id, floor_name, project_id, project_name, zone_name = row
    return {
        "floor_id": floor_id,
        "floor_name": floor_name,
        "project_id": project_id,
        "project_name": project_name,
        "zone_name": zone_name,
        "location_code": frame.location.code if frame.location else None,
    }
