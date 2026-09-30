"""Recording what walkthrough detection found, and splitting an upload on an operator's word.

`app.vision.grouping` decides where a walk stopped. This module is the half that touches the
database: it stores the finding on the session during MAPPING, and — separately, only when a
person asks — carves the capture into the walkthroughs they confirmed.

**The split moves no bytes.** That is the central decision here and it is worth stating plainly,
because the obvious implementation is the other one. A new walkthrough could own copies of its
frames, thumbnails, previews and feature blobs, and then every session would be self-contained.
But copying cannot be made atomic with the transaction that re-points the rows: a process that
dies between the copy and the commit leaves objects with no row, and one that dies between the
commit and the delete leaves rows pointing at bytes that are about to go. Splitting a day's
capture would be a long, interruptible, half-undoable operation over hundreds of megabytes.

Re-pointing rows is none of those things. It is one transaction, it is instant, and it either
happened or it did not. The cost is that a child's frames reference objects under the parent's
storage prefix — so deletion has to stop assuming a session owns everything beneath its own
prefix. `app.services.captures.delete_session_storage` is where that is handled, and the sharing
is not new: a still frame has always pointed at the uploaded file's bytes rather than a copy.

**The first walkthrough stays in the original capture, on its original floor.** Frames' storage
keys embed the floor the capture was created on, and nothing rewrites a key, so a session whose
`floor_id` moved would look for its own bytes in the wrong place at deletion time. The operator
told the platform which floor they were starting on when they created the capture, so the first
segment is the one least in need of correcting — but this is a real restriction and the API says
so rather than silently ignoring a floor it was given.

**What the split invalidates.** A track estimated straight through a lift ride is not a track,
so the mapping the parent already has is cleared along with its placement — for the parent as
well as the children. The frames keep their quality scores and their ORB features, which were
computed per frame and are unaffected by where the frame is filed; the child inherits those
stages as completed jobs so that re-processing it runs mapping and not extraction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.membership import assert_member
from app.core.enums import CaptureSessionStatus, JobStatus, JobType, MappingConfidence
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.ids import new_capture_session_id
from app.core.logging import log_event
from app.models import (
    CaptureFile,
    CaptureFrame,
    CapturePath,
    CaptureSession,
    Floor,
    ProcessingJob,
    User,
    Zone,
)
from app.vision.grouping import GroupingReport, Link, WalkthroughDetector

#: Walkthroughs one upload may be cut into at once.
#:
#: Not a technical limit — a bound on a request that creates sessions, enqueues jobs and rewrites
#: every frame row in the capture. A day's work is a handful of floors; a request asking for
#: eighty segments is a client bug or an operator who means something else, and either way the
#: kinder answer is a refusal that says so.
MAX_SEGMENTS = 20

#: Stages whose work belongs to the frame rather than to the session, and so survives a split.
#:
#: Validation and metadata read the uploaded file; extraction produced these exact frames;
#: quality scored each one on its own; features were detected per frame. None of that changes
#: because the frame is now filed under a different capture. Recording them as completed on the
#: child is what makes `POST /captures/{id}/process` run mapping instead of extracting a second
#: full copy of the parent's video.
INHERITED_STAGES: tuple[JobType, ...] = (
    JobType.VALIDATE,
    JobType.METADATA,
    JobType.FRAME_EXTRACTION,
    JobType.QUALITY,
    JobType.FEATURES,
)


# ---- recording the finding ---------------------------------------------------
def record_analysis(
    db: Session,
    session: CaptureSession,
    frames: list[CaptureFrame],
    per_pair: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run walkthrough detection over what MAPPING already computed, and store the result.

    ``frames`` are the keyframes mapping estimated a track through, in order, and ``per_pair[i]``
    is its own summary of the pair ``(frames[i], frames[i + 1])``. Both come from the caller
    because re-deriving either would mean matching every consecutive pair a second time.
    """
    if len(frames) < 2 or len(per_pair) != len(frames) - 1:
        # Not an error: a one-frame capture is legitimately ungroupable, and a caller that
        # cannot supply the pairs simply has nothing for this to read.
        session.grouping_report = None
        return {"analysed": False, "reason": "Too few mapped frames to look for breaks"}

    links = [
        Link(
            gap_seconds=max(
                (frames[index + 1].captured_at - frames[index].captured_at).total_seconds(), 0.0
            ),
            inlier_count=int(pair.get("inliers") or 0),
            match_count=int(pair.get("matches") or 0),
            features_before=int(frames[index].feature_count or 0),
            features_after=int(frames[index + 1].feature_count or 0),
        )
        for index, pair in enumerate(per_pair)
    ]

    report = WalkthroughDetector().detect(links)
    payload = describe(report, frames, bounds=_sequence_bounds(db, session.id))
    session.grouping_report = payload
    db.flush()

    if not report.looks_like_one_walkthrough:
        log_event(
            "grouping.breaks_found",
            session_id=session.id,
            project_id=session.project_id,
            segments=len(report.segments),
            boundaries=len(report.proposed),
        )
    return payload


def describe(
    report: GroupingReport, frames: list[CaptureFrame], *, bounds: tuple[int, int]
) -> dict[str, Any]:
    """Turn positions in the mapped-frame list into something addressable by an API caller.

    The detector counts in keyframes, because those are the frames it had evidence about. The
    operator and the split count in ``sequence_index``, which covers every frame in the capture
    including the duplicates and the unusable ones that mapping skipped. Those in-between frames
    are attached to the segment *before* the break: the last thing known about them is that the
    walk had not yet stopped.
    """
    first_sequence, last_sequence = bounds
    payload = report.to_dict()

    for entry, boundary in zip(payload["boundaries"], report.boundaries, strict=True):
        before = frames[boundary.link_index]
        after = frames[boundary.link_index + 1]
        entry["after_frame_id"] = before.id
        entry["before_frame_id"] = after.id
        entry["after_sequence_index"] = before.sequence_index
        entry["breaks_at"] = after.captured_at.isoformat()

    cuts = [frames[boundary.link_index].sequence_index for boundary in report.proposed]
    payload["segments"] = _segment_payload(cuts, first_sequence, last_sequence)
    return payload


def _segment_payload(cuts: list[int], first: int, last: int) -> list[dict[str, Any]]:
    spans: list[tuple[int, int]] = []
    start = first
    for cut in cuts:
        spans.append((start, cut))
        start = cut + 1
    spans.append((start, last))
    return [
        {
            "index": position + 1,
            "start_sequence_index": span_start,
            "end_sequence_index": span_end,
            "frame_count": span_end - span_start + 1,
        }
        for position, (span_start, span_end) in enumerate(spans)
    ]


def _sequence_bounds(db: Session, session_id: str) -> tuple[int, int]:
    row = db.execute(
        select(func.min(CaptureFrame.sequence_index), func.max(CaptureFrame.sequence_index)).where(
            CaptureFrame.session_id == session_id
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


# ---- splitting ---------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SegmentPlan:
    """One walkthrough an operator has confirmed, and where they say it was."""

    start_sequence_index: int
    end_sequence_index: int
    #: ``None`` keeps the capture's own floor. Ignored for the first segment, which cannot move.
    floor_id: str | None = None
    zone_id: str | None = None
    name: str | None = None


@dataclass(slots=True)
class SplitOutcome:
    parent_id: str
    created: list[CaptureSession] = field(default_factory=list)
    moved_frames: int = 0
    cleared_track: bool = False
    note: str = ""


def split_session(
    db: Session,
    session: CaptureSession,
    plans: list[SegmentPlan],
    user: User,
) -> SplitOutcome:
    """Cut one capture into the walkthroughs an operator confirmed.

    The plan is taken at face value rather than checked against the stored proposal. The operator
    can see the frames; the detector cannot see the building, and a boundary it declined to
    propose is still a boundary if the person walking knows it was one.
    """
    if session.split_children:
        raise ConflictError(
            "This capture has already been split. Delete the walkthroughs it produced before "
            "splitting it again.",
            session_id=session.id,
            walkthroughs=[child.id for child in session.split_children],
        )

    frames = list(
        db.scalars(
            select(CaptureFrame)
            .where(CaptureFrame.session_id == session.id)
            .order_by(CaptureFrame.sequence_index)
        )
    )
    if not frames:
        raise ValidationError("This capture has no frames to split", session_id=session.id)

    spans = _validated_spans(plans, frames)
    floors = _resolved_floors(db, session, plans, user)

    by_sequence = {frame.sequence_index: frame for frame in frames}
    outcome = SplitOutcome(parent_id=session.id)

    # The first segment is the capture itself: its frames do not move, and neither does its
    # floor. Everything after it becomes a capture of its own.
    for position, (plan, (start, end)) in enumerate(zip(plans, spans, strict=True), start=1):
        if position == 1:
            continue
        member = [by_sequence[index] for index in range(start, end + 1) if index in by_sequence]
        child = _new_session(db, session, floors[position - 1], plan, position, member)
        _move_frames(db, session, child, member)
        _inherit_stages(db, child, session)
        outcome.created.append(child)
        outcome.moved_frames += len(member)

    _drop_emptied_files(db, session)
    outcome.cleared_track = _invalidate_track(db, session)
    for child in outcome.created:
        _invalidate_track(db, child)

    # The proposal described a capture that no longer exists, and the capture now ends where its
    # first walkthrough did rather than where the day did.
    session.grouping_report = None
    kept = by_sequence.get(spans[0][1])
    if kept is not None:
        session.ended_at = kept.captured_at
    db.flush()

    outcome.note = (
        f"Split into {len(plans)} walkthroughs. The estimated track was cleared from all of "
        "them — a track that ran through the break was not measuring anything. Re-run "
        "processing on each to map it."
    )
    log_event(
        "capture.split",
        session_id=session.id,
        project_id=session.project_id,
        segments=len(plans),
        created=[child.id for child in outcome.created],
        moved_frames=outcome.moved_frames,
        actor_id=user.id,
    )
    return outcome


def _validated_spans(plans: list[SegmentPlan], frames: list[CaptureFrame]) -> list[tuple[int, int]]:
    """Every frame in exactly one segment, in order, or a refusal that names the hole."""
    if len(plans) < 2:
        raise ValidationError("A split needs at least two walkthroughs")
    if len(plans) > MAX_SEGMENTS:
        raise ValidationError(
            f"At most {MAX_SEGMENTS} walkthroughs can be split from one capture at a time; "
            f"{len(plans)} were asked for"
        )

    first, last = frames[0].sequence_index, frames[-1].sequence_index
    spans = [(plan.start_sequence_index, plan.end_sequence_index) for plan in plans]

    for position, (start, end) in enumerate(spans, start=1):
        if end < start:
            raise ValidationError(f"Walkthrough {position} ends before it starts")
    if spans[0][0] != first:
        raise ValidationError(
            f"The first walkthrough must start at the capture's first frame ({first})"
        )
    if spans[-1][1] != last:
        raise ValidationError(f"The last walkthrough must end at the capture's last frame ({last})")
    for position in range(1, len(spans)):
        if spans[position][0] != spans[position - 1][1] + 1:
            raise ValidationError(
                f"Walkthroughs {position} and {position + 1} do not meet: frames "
                f"{spans[position - 1][1] + 1}–{spans[position][0] - 1} would belong to neither"
            )

    present = {frame.sequence_index for frame in frames}
    for position, (start, end) in enumerate(spans, start=1):
        if not any(index in present for index in range(start, end + 1)):
            raise ValidationError(f"Walkthrough {position} contains no frames")
    return spans


def _resolved_floors(
    db: Session, session: CaptureSession, plans: list[SegmentPlan], user: User
) -> list[Floor]:
    """The floor each walkthrough goes to, refusing anything the operator cannot reach."""
    own = db.get(Floor, session.floor_id)
    if own is None:  # pragma: no cover - a session cannot outlive its floor
        raise NotFoundError("This capture's floor no longer exists")

    floors: list[Floor] = []
    for position, plan in enumerate(plans, start=1):
        if position == 1:
            if plan.floor_id is not None and plan.floor_id != own.id:
                raise ValidationError(
                    "The first walkthrough stays in this capture and keeps its floor. Its frames "
                    "are stored under that floor, and moving the row would not move them. Split "
                    "the rest out first, then correct this one.",
                    floor_id=own.id,
                )
            floors.append(own)
            continue
        if plan.floor_id is None or plan.floor_id == own.id:
            floors.append(own)
            continue
        floor = db.get(Floor, plan.floor_id)
        if floor is None:
            raise NotFoundError(f"Floor {plan.floor_id} not found")
        # Membership first, and a 404 rather than a refusal: a caller who is not in that floor's
        # project must not learn from the error that it exists.
        assert_member(db, user, floor.project_id, what="Floor")
        if floor.project_id != session.project_id:
            raise ValidationError(
                f"Walkthrough {position} was given a floor in another project", floor_id=floor.id
            )
        floors.append(floor)

    for position, (plan, floor) in enumerate(zip(plans, floors, strict=True), start=1):
        if plan.zone_id is None:
            continue
        zone = db.get(Zone, plan.zone_id)
        if zone is None or zone.floor_id != floor.id:
            raise ValidationError(
                f"Walkthrough {position}'s zone does not belong to the floor it was given",
                zone_id=plan.zone_id,
            )
    return floors


def _new_session(
    db: Session,
    parent: CaptureSession,
    floor: Floor,
    plan: SegmentPlan,
    position: int,
    frames: list[CaptureFrame],
) -> CaptureSession:
    started = frames[0].captured_at if frames else parent.started_at
    base = parent.name or parent.id
    child = CaptureSession(
        id=new_capture_session_id(started),
        project_id=parent.project_id,
        building_id=floor.building_id,
        floor_id=floor.id,
        zone_id=plan.zone_id,
        name=plan.name or f"{base} — walkthrough {position}",
        # The files were uploaded and the frames exist; what has not happened for *this* capture
        # is mapping. UPLOADED is the status that says exactly that, and it is the one the
        # process endpoint expects to act on.
        status=CaptureSessionStatus.UPLOADED,
        started_at=started,
        ended_at=frames[-1].captured_at if frames else parent.ended_at,
        operator_id=parent.operator_id,
        capture_source=parent.capture_source,
        camera_device_id=parent.camera_device_id,
        camera_label=parent.camera_label,
        frame_interval_seconds=parent.frame_interval_seconds,
        notes=parent.notes,
        gps_latitude=parent.gps_latitude,
        gps_longitude=parent.gps_longitude,
        split_from_session_id=parent.id,
        split_segment_index=position,
        frame_count=len(frames),
    )
    db.add(child)
    db.flush()
    return child


def _move_frames(
    db: Session, parent: CaptureSession, child: CaptureSession, frames: list[CaptureFrame]
) -> None:
    """Re-point frame rows at the new capture, giving it its own file rows on the way.

    A frame's ``file_id`` cannot keep pointing at the parent's row: deleting the parent cascades
    through its files to its files' frames, which would take the child's frames with it. So each
    parent file contributing frames here gets a counterpart on the child — same bytes, same
    metadata, its own row — and the frames move to it.
    """
    counterparts: dict[str, CaptureFile] = {}
    for frame in frames:
        counterpart = counterparts.get(frame.file_id)
        if counterpart is None:
            counterpart = _copy_file_row(db, frame.file_id, child)
            counterparts[frame.file_id] = counterpart
        frame.session_id = child.id
        frame.file_id = counterpart.id
    # Renumbered from zero so the child reads like any other capture — the viewer, the timeline
    # and `uq_frame_file_sequence` all count from the start of a session.
    for position, frame in enumerate(sorted(frames, key=lambda item: item.sequence_index)):
        frame.sequence_index = position

    child.file_count = len(counterparts)
    db.flush()

    parent.frame_count = int(
        db.scalar(select(func.count(CaptureFrame.id)).where(CaptureFrame.session_id == parent.id))
        or 0
    )


def _copy_file_row(db: Session, file_id: str, child: CaptureSession) -> CaptureFile:
    original = db.get(CaptureFile, file_id)
    if original is None:  # pragma: no cover - a frame cannot outlive its file
        raise NotFoundError(f"File {file_id} not found")
    counterpart = CaptureFile(
        session_id=child.id,
        original_filename=original.original_filename,
        # Deliberately the same object. The upload is one file that happens to contain several
        # walks; storing it twice would double a video's bytes to record that fact.
        storage_key=original.storage_key,
        content_type=original.content_type,
        file_size_bytes=original.file_size_bytes,
        checksum_sha256=original.checksum_sha256,
        kind=original.kind,
        projection=original.projection,
        width_px=original.width_px,
        height_px=original.height_px,
        duration_seconds=original.duration_seconds,
        fps=original.fps,
        captured_at=original.captured_at,
        camera_make=original.camera_make,
        camera_model=original.camera_model,
        gps_latitude=original.gps_latitude,
        gps_longitude=original.gps_longitude,
        gps_altitude_m=original.gps_altitude_m,
        metadata_json=original.metadata_json,
        validation_errors=original.validation_errors,
        validation_warnings=original.validation_warnings,
    )
    db.add(counterpart)
    db.flush()
    return counterpart


def _inherit_stages(db: Session, child: CaptureSession, parent: CaptureSession) -> None:
    """Record the per-frame stages as done, because for these frames they are."""
    now = datetime.now(UTC)
    for stage in INHERITED_STAGES:
        db.add(
            ProcessingJob(
                session_id=child.id,
                job_type=stage,
                status=JobStatus.COMPLETE,
                attempt=1,
                progress=1.0,
                started_at=now,
                finished_at=now,
                duration_seconds=0.0,
                message=f"Inherited from {parent.id} when that upload was split",
                result={"inherited_from": parent.id, "stage": str(stage)},
            )
        )
    db.flush()


def _invalidate_track(db: Session, session: CaptureSession) -> bool:
    """Throw away a track, and a placement, that spanned a break.

    Positions are the only thing a split makes false. Quality scores and ORB features are
    per-frame facts; location assignments are per-frame too and were made by a person or by a
    transform that has now been discarded — so the *frames* keep their locations and the session
    loses its estimate. What goes is the chain that ran through a discontinuity and the placement
    that was fitted to it.
    """
    had_track = session.mapping_confidence is not MappingConfidence.NONE or session.path is not None

    for frame in db.scalars(select(CaptureFrame).where(CaptureFrame.session_id == session.id)):
        frame.rel_x = None
        frame.rel_y = None
        frame.heading_deg = None
        frame.position_provenance = None
        frame.position_confidence = None

    path = db.scalar(select(CapturePath).where(CapturePath.session_id == session.id))
    if path is not None:
        db.delete(path)

    session.mapping_confidence = MappingConfidence.NONE
    session.mapping_confidence_score = None
    session.mapping_method = None
    session.loop_closure_report = None
    session.placement_method = None
    session.localization_report = None
    session.mapping_note = (
        "The estimated track was cleared when this upload was split into separate walkthroughs. "
        "Re-run processing to map this one on its own."
    )
    db.flush()
    return had_track


def _drop_emptied_files(db: Session, parent: CaptureSession) -> None:
    """Remove the parent's file rows whose frames all went to a walkthrough.

    Every walkthrough gets a counterpart row over each file it drew frames from, because a frame
    cannot keep pointing at the parent's row — deleting the parent cascades through its files to
    their frames and would take the walkthrough's imagery with it. Where the whole file went to
    one walkthrough, that leaves the parent holding a row with nothing behind it: an operator
    reading the file list sees five uploads contributing zero frames, which reads as a failed
    import rather than a completed split.

    Only the row goes. The bytes are still named by the counterpart, and nothing here touches
    storage — see `captures.delete_file_storage` for the rule about keys two rows share.
    """
    orphans = db.scalars(
        select(CaptureFile)
        .where(CaptureFile.session_id == parent.id)
        .where(~select(CaptureFrame.id).where(CaptureFrame.file_id == CaptureFile.id).exists())
    ).all()
    for orphan in orphans:
        db.delete(orphan)
    db.flush()

    parent.file_count = int(
        db.scalar(select(func.count(CaptureFile.id)).where(CaptureFile.session_id == parent.id))
        or 0
    )
    db.flush()
