"""Placing a capture by recognising a floor that has been captured before (§15, §46).

The first walk of a floor has to be anchored by hand: nothing else has ever been there, so
nothing can say where the walk was. Every walk after it is a different problem — the floor is
already covered in frames whose plan coordinates are known — and this module is the answer to
that one. It matches the new capture's frames against the placed frames of earlier captures, and
where a frame is recognised as standing in the same spot as an earlier one, that spot becomes an
anchor. Three of those and the whole track places itself, with nobody dragging anything.

Two outcomes are worth distinguishing, and they are reported separately:

``transform``
    Three or more recognised frames, far enough apart on the plan, and a similarity fit whose
    residual holds. Every frame of the walk is placed, exactly as manual anchoring would.

``recognised``
    Fewer matches than that, or a fit that did not hold. Only the frames actually recognised are
    placed — each one directly, from the evidence for that frame, with nothing extrapolated. A
    walk with two recognised frames genuinely knows where those two were; it does not know where
    the other forty were, and inventing them from a fit that cannot be checked would be the
    failure mode this whole codebase is written against.

A recognised frame **joins the existing location** rather than creating a second point beside it.
That is not a tidiness measure: comparing a place across dates is the product's central feature,
and it works by two frames sharing a location row. A capture placed two pixels from last month's
capture compares against nothing.

Frames placed by the fitted transform get new locations instead, because a fit says where a
frame probably was, not that it was where some earlier frame was. Snapping those to nearby
existing points would turn an estimate into a claim of identity.

The service flushes and never commits. It is called both from a route and from inside the
MAPPING pipeline stage, and the stage runner owns a transaction it rolls back when a stage
fails — a commit here would leave half a stage's work behind after that rollback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import DataProvenance
from app.core.errors import NotFoundError
from app.core.logging import log_event
from app.models import CaptureFrame, CaptureLocation, CaptureSession, Floor
from app.services import captures as capture_service
from app.storage import get_storage
from app.vision.features import deserialize_features
from app.vision.mapbuilder import MapBuilder
from app.vision.relocalize import (
    MIN_TRANSFORM_MATCHES,
    RecognitionReport,
    ReferenceFrame,
    Relocalizer,
    anchors_are_usable,
)
from app.vision.types import FeatureSet, RelativePose

#: Placed frames from earlier captures to consider as references, newest first.
#:
#: A cap rather than the whole history, because recognition costs a hash comparison per reference
#: and the newest captures are both the most likely to match and the most likely to still be
#: true: a floor changes, and a frame from eight months ago may show a room that no longer looks
#: anything like itself.
MAX_REFERENCE_FRAMES = 400

#: What `placement_method` records when this service did the placing.
PLACEMENT_METHOD = "cross_session"


@dataclass(frozen=True, slots=True)
class LocalizationOutcome:
    """What happened, in enough detail to argue with."""

    ok: bool
    #: ``transform``, ``recognised`` or ``none``.
    mode: str
    #: Why, in one machine-readable token. ``mode`` alone is not enough: ``none`` covers a floor
    #: nobody has captured before, a walk nothing recognised, and a capture that is already
    #: placed, and those call for three different things from whoever reads it. A caller that has
    #: to infer the difference from `note` will infer it wrong the first time the wording changes.
    reason: str
    note: str
    placed_frames: int = 0
    matched_frames: int = 0
    reused_locations: int = 0
    new_locations: int = 0
    references_considered: int = 0
    transform: dict[str, float] | None = None
    report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "reason": self.reason,
            "note": self.note,
            "placed_frames": self.placed_frames,
            "matched_frames": self.matched_frames,
            "reused_locations": self.reused_locations,
            "new_locations": self.new_locations,
            "references_considered": self.references_considered,
            "transform": self.transform,
            "recognition": self.report,
        }


def localize_session_to_plan(
    db: Session,
    session: CaptureSession,
    *,
    user_id: str | None = None,
    replace_existing: bool = False,
) -> LocalizationOutcome:
    """Place a capture from earlier captures of the same floor, or say why it could not be.

    Returns rather than raises for every ordinary refusal — no earlier capture, nothing
    recognised, a fit that did not hold. These are expected answers on a floor being captured for
    the first time, and the MAPPING stage calls this on every capture.
    """
    frames = _query_frames(db, session)
    if len(frames) < 1:
        return LocalizationOutcome(
            ok=False,
            mode="none",
            reason="not_processed",
            note="This capture has no frames with features to match. Run processing first.",
        )

    already_placed = [frame for frame in frames if frame.location_id]
    if already_placed and not replace_existing:
        return LocalizationOutcome(
            ok=False,
            mode="none",
            reason="already_placed",
            note=(
                f"{len(already_placed)} frame(s) of this capture are already placed. "
                "Automatic placement will not overwrite them."
            ),
        )

    references = _reference_frames(db, session)
    if not references:
        return _recorded(
            db,
            session,
            LocalizationOutcome(
                ok=False,
                mode="none",
                reason="no_references",
                note=(
                    "No earlier capture of this floor has been placed on the plan, so there is "
                    "nothing to recognise this walk against. Anchor it by hand once and later "
                    "captures can place themselves."
                ),
            ),
        )

    storage = get_storage()
    queries: list[FeatureSet] = []
    hashes: list[str] = []
    for frame in frames:
        queries.append(deserialize_features(storage.read_bytes(str(frame.feature_blob_key))))
        hashes.append(frame.perceptual_hash or "")

    from app.vision import get_vision_engine

    engine = get_vision_engine()
    report = Relocalizer(engine.matcher).recognise(queries, hashes, references)

    if not report.matches:
        return _recorded(
            db,
            session,
            LocalizationOutcome(
                ok=False,
                mode="none",
                reason="no_matches",
                note=(
                    f"No frame of this walk was recognised against {len(references)} placed "
                    "frame(s) from earlier captures of this floor. Place it by hand."
                ),
                references_considered=len(references),
                report=report.to_dict(),
            ),
        )

    usable, reason = anchors_are_usable(report.matches)
    if usable:
        placed = _place_whole_track(db, session, frames, report, user_id=user_id)
        if isinstance(placed, LocalizationOutcome):
            return placed
        # It fell back, and said why. Carrying that sentence through matters: "three frames were
        # recognised" and "three frames were recognised and the fit through them was rejected"
        # call for different things from the operator, and the second one is the interesting
        # case — it means the recognitions disagree with each other or with the estimated track.
        reason = placed

    # Too few matches to fit a transform, or a fit that did not hold. What was recognised is
    # still known, and is placed on its own evidence.
    return _place_recognised_only(db, session, frames, report, note_prefix=reason)


# ---- the two outcomes --------------------------------------------------------
def _place_whole_track(
    db: Session,
    session: CaptureSession,
    frames: list[CaptureFrame],
    report: RecognitionReport,
    *,
    user_id: str | None,
) -> LocalizationOutcome | str:
    """Fit a similarity transform from the recognised frames and place every frame.

    Returns a sentence instead of an outcome when the track cannot carry a transform — the
    mapper estimated too little, or the fit through the recognised frames was rejected. The
    caller places what was recognised and passes the sentence on, because "nothing else could be
    placed" and "the fit through these was rejected" are very different pieces of news.
    """
    poses = [
        RelativePose(
            frame_index=index,
            x=float(frame.rel_x),
            y=float(frame.rel_y),
            heading_deg=float(frame.heading_deg or 0.0),
            step_confidence=float(frame.position_confidence or 0.0),
            cumulative_confidence=float(frame.position_confidence or 0.0),
        )
        for index, frame in enumerate(frames)
        if frame.rel_x is not None and frame.rel_y is not None
    ]
    if len(poses) < MIN_TRANSFORM_MATCHES:
        return (
            "The mapper estimated positions for too few frames of this walk to carry a "
            "transform across it."
        )

    anchors = [
        (match.query_index, match.plan_x, match.plan_y)
        for match in report.matches
        if frames[match.query_index].rel_x is not None
    ]
    if len(anchors) < MIN_TRANSFORM_MATCHES:
        return (
            f"{len(anchors)} of the recognised frames have an estimated position, which is too "
            "few to fit a transform over the walk."
        )

    fit = MapBuilder().place_track(poses, anchors)
    if not fit.ok or fit.transform is None:
        # Worth reading closely when it happens: the recognised frames disagree either with each
        # other or with the shape the mapper estimated, and applying the fit anyway would spread
        # that disagreement over every frame while still looking like a measurement.
        return f"The fit through the recognised frames was rejected. {fit.note}"

    floor = _floor_of(db, session)
    matched_by_index = {match.query_index: match for match in report.matches}

    reused = 0
    created = 0
    placed_indices: set[int] = set()
    for entry in fit.placed:
        index = int(entry["frame_index"])
        frame = frames[index]
        placed_indices.add(index)
        match = matched_by_index.get(index)
        if match is not None:
            # Recognised outright. Join the existing point rather than the transform's estimate
            # of where it is: direct evidence beats an extrapolation, and sharing the location
            # row is what makes this frame comparable with the earlier capture's.
            location = db.get(CaptureLocation, match.location_id)
            if location is not None:
                _bind(frame, location, confidence=match.confidence)
                reused += 1
                continue
        location = capture_service.create_location(
            db,
            floor,
            x=float(entry["x"]),
            y=float(entry["y"]),
            zone_id=session.zone_id,
            provenance=DataProvenance.ESTIMATED,
            confidence=float(entry["confidence"]),
            created_by_id=user_id,
            commit=False,
        )
        _bind(frame, location, confidence=float(entry["confidence"]))
        created += 1

    # A frame the mapper never estimated has no pose, so the transform could not carry it — but
    # if it was recognised outright it does not need carrying: that match is a position on its
    # own. Placing it here means a walk whose mapping partly failed still keeps what it knew.
    for index, match in matched_by_index.items():
        if index in placed_indices:
            continue
        location = db.get(CaptureLocation, match.location_id)
        if location is None:
            continue
        _bind(frames[index], location, confidence=match.confidence)
        reused += 1

    note = (
        f"Placed automatically from {len(anchors)} frames recognised in earlier captures of this "
        f"floor; residual {fit.transform.rms_error_px:.1f}px. Positions are estimated."
    )
    outcome = LocalizationOutcome(
        ok=True,
        mode="transform",
        reason="placed",
        note=note,
        placed_frames=reused + created,
        matched_frames=len(report.matches),
        reused_locations=reused,
        new_locations=created,
        references_considered=report.references_considered,
        transform=fit.transform.to_dict(),
        report=report.to_dict(),
    )
    _record(db, session, outcome)
    return outcome


def _place_recognised_only(
    db: Session,
    session: CaptureSession,
    frames: list[CaptureFrame],
    report: RecognitionReport,
    *,
    note_prefix: str,
) -> LocalizationOutcome:
    """Place exactly the frames that were recognised, and nothing else."""
    reused = 0
    for match in report.matches:
        location = db.get(CaptureLocation, match.location_id)
        if location is None:
            continue
        _bind(frames[match.query_index], location, confidence=match.confidence)
        reused += 1

    note = (
        f"{note_prefix} The recognised frames were placed on the points they were recognised at; "
        "the rest of the walk still needs anchoring."
    )
    outcome = LocalizationOutcome(
        ok=reused > 0,
        mode="recognised",
        reason="placed" if reused else "no_matches",
        note=note,
        placed_frames=reused,
        matched_frames=len(report.matches),
        reused_locations=reused,
        references_considered=report.references_considered,
        report=report.to_dict(),
    )
    _record(db, session, outcome)
    return outcome


# ---- shared ------------------------------------------------------------------
def _bind(frame: CaptureFrame, location: CaptureLocation, *, confidence: float) -> None:
    """Attach a frame to a plan location, as an estimate.

    Deliberately not `capture_service.assign_frame_to_location`, which stamps `MANUAL` and
    full confidence: that is the truth when a person dropped the pin and a fabrication here.
    """
    frame.location_id = location.id
    frame.position_provenance = DataProvenance.ESTIMATED
    frame.position_confidence = confidence


def _record(db: Session, session: CaptureSession, outcome: LocalizationOutcome) -> None:
    """Persist what recognition did, including when it did nothing.

    A capture with no report is one where recognition never ran. A capture whose report says
    ``references_considered: 0`` is one where it ran and the floor had never been walked before.
    Those are different facts, and only the second one tells an operator that anchoring this
    capture by hand will make every later capture of the floor place itself.
    """
    from app.processing.stages import build_capture_path

    session.localization_report = outcome.to_dict()
    if outcome.placed_frames:
        session.placement_method = PLACEMENT_METHOD
        db.flush()
        build_capture_path(db, session)
    db.flush()

    log_event(
        "mapping.cross_session",
        session_id=session.id,
        mode=outcome.mode,
        placed_frames=outcome.placed_frames,
        matched_frames=outcome.matched_frames,
        reused_locations=outcome.reused_locations,
        new_locations=outcome.new_locations,
        references_considered=outcome.references_considered,
    )


def _recorded(
    db: Session, session: CaptureSession, outcome: LocalizationOutcome
) -> LocalizationOutcome:
    """Persist an outcome that changed nothing, and hand it back."""
    _record(db, session, outcome)
    return outcome


def _floor_of(db: Session, session: CaptureSession) -> Floor:
    floor = db.get(Floor, session.floor_id)
    if floor is None:
        raise NotFoundError("The capture's floor no longer exists")
    return floor


def _query_frames(db: Session, session: CaptureSession) -> list[CaptureFrame]:
    """Keyframes of this capture that have features to match with."""
    return list(
        db.scalars(
            select(CaptureFrame)
            .where(
                CaptureFrame.session_id == session.id,
                CaptureFrame.is_keyframe.is_(True),
                CaptureFrame.feature_blob_key.is_not(None),
            )
            .order_by(CaptureFrame.sequence_index)
        )
    )


def _reference_frames(db: Session, session: CaptureSession) -> list[ReferenceFrame]:
    """Placed frames from *other* captures of the same floor, newest first.

    Same floor, because plan coordinates mean nothing across floors — pixel (400, 300) is a
    different room on every level. Other sessions, because recognising a walk against itself
    tells it only what it already knows.
    """
    storage = get_storage()
    rows = db.execute(
        select(CaptureFrame, CaptureLocation)
        .join(CaptureLocation, CaptureFrame.location_id == CaptureLocation.id)
        .join(CaptureSession, CaptureFrame.session_id == CaptureSession.id)
        .where(
            CaptureSession.floor_id == session.floor_id,
            CaptureFrame.session_id != session.id,
            CaptureFrame.feature_blob_key.is_not(None),
            CaptureFrame.perceptual_hash.is_not(None),
        )
        .order_by(CaptureFrame.captured_at.desc())
        .limit(MAX_REFERENCE_FRAMES)
    ).all()

    cache: dict[str, FeatureSet] = {}

    def loader(key: str):
        def load() -> FeatureSet:
            if key not in cache:
                cache[key] = deserialize_features(storage.read_bytes(key))
            return cache[key]

        return load

    return [
        ReferenceFrame(
            frame_id=frame.id,
            session_id=frame.session_id,
            plan_x=float(location.x),
            plan_y=float(location.y),
            perceptual_hash=str(frame.perceptual_hash),
            location_id=location.id,
            load_features=loader(str(frame.feature_blob_key)),
        )
        for frame, location in rows
    ]


__all__ = [
    "MAX_REFERENCE_FRAMES",
    "PLACEMENT_METHOD",
    "LocalizationOutcome",
    "localize_session_to_plan",
]
