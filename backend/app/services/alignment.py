"""Pinning an estimated track to a floor plan (§14, §15).

The mapping stage recovers where the operator walked *relative to where they started*: a track
with a shape but no origin, no compass bearing and — because the estimate is monocular — no
scale. Nothing about it says which corner of the building it belongs in, or whether the loop it
traced is four metres across or forty.

Anchoring is what supplies the three missing things at once. The operator names two or more
frames and drops each on the point of the plan where it was taken; a similarity transform is
fitted to those correspondences and applied to the whole track, so every other frame lands
without being placed by hand.

Deliberately a **similarity** transform — rotation, uniform scale, translation — and not an
affine or projective one. Allowing shear or independent axis scaling would let the fit absorb a
badly-placed anchor by quietly distorting the building, and it would still report a small
residual while doing it. A similarity transform cannot hide that error: a bad anchor shows up as
a large residual, which is what the caller is told about and what makes the refusal below
meaningful.

The transform is the reachable half of `app.vision.mapbuilder`, which did the maths already and
had no caller.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import DataProvenance
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import log_event
from app.models import CaptureFrame, CaptureSession, Floor
from app.services import captures as capture_service
from app.vision.mapbuilder import MapBuilder
from app.vision.types import RelativePose


@dataclass(frozen=True, slots=True)
class Anchor:
    """One frame the operator has identified on the plan."""

    frame_id: str
    plan_x: float
    plan_y: float


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    placed_frames: int
    scale: float
    rotation_deg: float
    rms_error_px: float
    note: str


def align_session_to_plan(
    db: Session,
    session: CaptureSession,
    anchors: list[Anchor],
    *,
    user_id: str | None = None,
) -> AlignmentResult:
    """Place every frame of a capture on the floor plan from a handful of manual anchors.

    Raises rather than returning a half-placed track: a fit that does not hold is worse than no
    fit at all, because the positions look measured and the operator has no way to tell that the
    transform behind them was rejected.
    """
    frames = list(
        db.scalars(
            select(CaptureFrame)
            .where(CaptureFrame.session_id == session.id)
            .order_by(CaptureFrame.sequence_index)
        )
    )
    if not frames:
        raise ValidationError("This capture has no frames to place", session_id=session.id)

    # The mapping stage writes rel_x/rel_y; a frame without them was never estimated and cannot
    # be carried along by the transform.
    estimated = [frame for frame in frames if frame.rel_x is not None and frame.rel_y is not None]
    if not estimated:
        raise ValidationError(
            "This capture has no estimated track to place. Run processing first, or place the "
            "frames individually.",
            session_id=session.id,
        )

    index_of = {frame.id: position for position, frame in enumerate(estimated)}
    poses = [
        RelativePose(
            frame_index=position,
            x=float(frame.rel_x or 0.0),
            y=float(frame.rel_y or 0.0),
            heading_deg=float(frame.heading_deg or 0.0),
            step_confidence=float(frame.position_confidence or 0.0),
            cumulative_confidence=float(frame.position_confidence or 0.0),
        )
        for position, frame in enumerate(estimated)
    ]

    triples: list[tuple[int, float, float]] = []
    for anchor in anchors:
        position = index_of.get(anchor.frame_id)
        if position is None:
            # Naming the frame is worth the extra message: the usual cause is anchoring a frame
            # that has no estimated position, which is indistinguishable from a typo otherwise.
            raise ValidationError(
                f"Frame {anchor.frame_id} is not part of this capture's estimated track",
                session_id=session.id,
            )
        triples.append((position, anchor.plan_x, anchor.plan_y))

    fit = MapBuilder().place_track(poses, triples)
    if not fit.ok or fit.transform is None:
        # `place_track` explains itself — too few anchors, collinear anchors, or a residual too
        # large to trust. Passing its own words through beats restating them less precisely.
        raise ValidationError(fit.note, session_id=session.id)

    floor = db.get(Floor, session.floor_id)
    if floor is None:
        raise NotFoundError("The capture's floor no longer exists")

    placed = 0
    for entry in fit.placed:
        frame = estimated[int(entry["frame_index"])]
        location = capture_service.create_location(
            db,
            floor,
            x=entry["x"],
            y=entry["y"],
            zone_id=session.zone_id,
            # ESTIMATED, not MEASURED (§46): these came out of a transform fitted to a handful of
            # human-placed points, and the confidence travels with them so the UI can say so.
            provenance=DataProvenance.ESTIMATED,
            confidence=entry["confidence"],
            created_by_id=user_id,
        )
        capture_service.assign_frame_to_location(db, frame, location)
        placed += 1

    db.commit()

    log_event(
        "mapping.anchored",
        session_id=session.id,
        anchors=len(triples),
        placed_frames=placed,
        rms_error_px=round(fit.transform.rms_error_px, 2),
        scale=round(fit.transform.scale, 4),
        rotation_deg=round(fit.transform.rotation_deg, 2),
    )
    return AlignmentResult(
        placed_frames=placed,
        scale=fit.transform.scale,
        rotation_deg=fit.transform.rotation_deg,
        rms_error_px=fit.transform.rms_error_px,
        note=fit.note,
    )
