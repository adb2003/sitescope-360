"""Report data builders (§29).

Builders return plain dicts. Rendering is separate, which is why the same computed numbers can be
served as JSON, downloaded as CSV, or laid out as a PDF without recomputing — and why a stored
report can be re-rendered in another format later from ``reports.payload``.

Every report carries a ``provenance`` block. A construction report that silently mixes measured
counts with estimated positions is worse than no report, so each section states which it is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import AnnotationStatus, CaptureSessionStatus, ReportType
from app.core.errors import NotFoundError, ValidationError
from app.models import (
    Annotation,
    Building,
    CaptureFrame,
    CaptureLocation,
    CaptureSession,
    Floor,
    Project,
    QualityResult,
    User,
    Zone,
)
from app.services import monitoring
from app.services.assets import sign_url
from app.services.relocalization import PLACEMENT_METHOD as PLACEMENT_METHOD_CROSS_SESSION

PROVENANCE_NOTE = {
    "capture_counts": "measured — counted from processed captures",
    "quality_scores": "measured — classical CV metrics, not a learned model",
    "coverage": (
        "manual — ratio of visited capture locations to administrator-declared expected "
        "capture points; square footage is apportioned, not measured from imagery"
    ),
    # Replaced per report by `_positions_provenance`, which counts what actually happened to
    # the captures in scope. Kept as the fallback for a report with nothing to count.
    "positions": (
        "estimated or manual — relative visual positioning is scale-free; manually placed "
        "points are exact on the floor plan"
    ),
    "progress_percentage": "not available — progress detection is a Phase 3 capability",
}


def build_report(
    db: Session,
    *,
    report_type: ReportType,
    project_id: str,
    floor_id: str | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")
    if floor_id and db.get(Floor, floor_id) is None:
        raise NotFoundError(f"Floor {floor_id} not found")

    if period_start and period_end and period_end < period_start:
        raise ValidationError("period_end cannot be before period_start")

    header = {
        "project": {
            "id": project.id,
            "name": project.name,
            "code": project.code,
            "client": project.client,
            "location": project.location,
            "status": str(project.status),
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "period": {
            "start": period_start.isoformat() if period_start else None,
            "end": period_end.isoformat() if period_end else None,
        },
        "provenance": {
            **PROVENANCE_NOTE,
            "positions": _positions_provenance(db, project, floor_id, period_start, period_end),
        },
        "platform": "SiteScope 360",
        "independence_note": (
            "Generated entirely by SiteScope 360. No third-party construction-monitoring "
            "platform was used to produce these figures."
        ),
    }

    builders = {
        ReportType.PROJECT_SUMMARY: _project_summary,
        ReportType.FLOOR_COVERAGE: _floor_coverage,
        ReportType.CAPTURE_HISTORY: _capture_history,
        ReportType.ANNOTATION_LOG: _annotation_log,
    }
    builder = builders[report_type]
    body = builder(db, project, floor_id, period_start, period_end)
    return {"report_type": str(report_type), **header, **body}


def _describe_placement(session: CaptureSession, placed_frames: int) -> str:
    """One capture's route onto the floor plan, in words rather than a token.

    ``placed_frames`` is passed rather than inferred because a null ``placement_method`` means
    two different things — frames bound to points one at a time in the editor, or a capture
    nobody has placed at all — and a report that cannot tell those apart is no use to a reader
    deciding whether to trust a position.
    """
    if session.placement_method == "manual_anchors":
        return "anchored by hand"
    if session.placement_method == PLACEMENT_METHOD_CROSS_SESSION:
        report = session.localization_report or {}
        generation = report.get("generation")
        suffix = f" ({generation} hop(s) from a hand-placed point)" if generation else ""
        return f"recognised from earlier captures{suffix}"
    return "placed frame by frame" if placed_frames else "not placed"


def _positions_provenance(
    db: Session,
    project: Project,
    floor_id: str | None,
    period_start: date | None,
    period_end: date | None,
) -> str:
    """Say how the positions *in this report* came about, rather than how they can come about.

    The static note this replaces was written when there were two answers — a person placed the
    point, or a transform they anchored did — and it was complete. Cross-session recognition
    added a third, and one that carries a caveat the other two do not: a recognised position
    matches an earlier capture's point to within a viewpoint tolerance, and a chain of those
    compounds. A report is the artefact that leaves the building and reaches people who will
    never see the interface explaining any of this, so it states what happened here, with
    numbers, rather than describing the range of things the software can do (§46).
    """
    scope = [CaptureSession.project_id == project.id]
    if floor_id:
        scope.append(CaptureSession.floor_id == floor_id)
    if period_start:
        scope.append(func.date(CaptureSession.started_at) >= period_start)
    if period_end:
        scope.append(func.date(CaptureSession.started_at) <= period_end)

    placed_frames = (
        select(CaptureFrame.session_id, func.count(CaptureFrame.id).label("placed"))
        .where(CaptureFrame.location_id.is_not(None))
        .group_by(CaptureFrame.session_id)
        .subquery()
    )
    rows = db.execute(
        select(CaptureSession.placement_method, func.coalesce(placed_frames.c.placed, 0))
        .outerjoin(placed_frames, placed_frames.c.session_id == CaptureSession.id)
        .where(*scope)
    ).all()

    if not rows:
        return PROVENANCE_NOTE["positions"]

    anchored = recognised = by_hand = unplaced = 0
    for method, placed in rows:
        if not placed:
            unplaced += 1
        elif method == "manual_anchors":
            anchored += 1
        elif method == PLACEMENT_METHOD_CROSS_SESSION:
            recognised += 1
        else:
            # Placed, but by neither route that records itself: the frames were bound to points
            # one at a time in the floor-plan editor.
            by_hand += 1

    if anchored + recognised + by_hand == 0:
        return (
            f"not placed — none of the {len(rows)} captures in this report has been positioned "
            "on a floor plan."
        )

    parts = []
    if anchored:
        parts.append(f"{anchored} anchored by hand")
    if recognised:
        parts.append(f"{recognised} recognised against earlier captures of the same floor")
    if by_hand:
        parts.append(f"{by_hand} placed frame by frame")
    if unplaced:
        parts.append(f"{unplaced} not placed")

    note = (
        f"estimated — of {len(rows)} captures here, " + ", ".join(parts) + ". "
        "Relative visual positioning is scale-free, so a track's position comes from the points "
        "it was fitted to rather than from measurement."
    )

    if recognised:
        depth = (
            db.scalar(
                select(func.max(CaptureLocation.placement_generation))
                .select_from(CaptureLocation)
                .join(CaptureFrame, CaptureFrame.location_id == CaptureLocation.id)
                .join(CaptureSession, CaptureSession.id == CaptureFrame.session_id)
                .where(*scope)
            )
            or 0
        )
        note += (
            " A recognised position matches an earlier capture's point to within a viewpoint "
            "tolerance rather than exactly"
        )
        note += (
            f", and the furthest any point here sits from one a person placed is {depth} hop(s) "
            "of automatic recognition."
            if depth
            else ", though every point used here was placed by a person or a fit they anchored."
        )

    return note


# ---- individual reports ------------------------------------------------------
def _project_summary(
    db: Session,
    project: Project,
    floor_id: str | None,
    period_start: date | None,
    period_end: date | None,
) -> dict[str, Any]:
    dashboard = monitoring.build_dashboard(db, project.id)
    timeline = monitoring.compute_timeline(db, project.id, limit=90)
    stale = monitoring.find_stale_areas(db, project.id)

    entries = [
        entry
        for entry in timeline.entries
        if _in_period(entry.capture_date, period_start, period_end)
    ]

    return {
        "title": f"Project summary — {project.name}",
        "totals": {
            "buildings": dashboard.building_count,
            "floors": dashboard.floor_count,
            "zones": dashboard.zone_count,
            "capture_locations": dashboard.location_count,
            "capture_sessions": dashboard.session_count,
            "frames": dashboard.frame_count,
            "open_annotations": dashboard.annotation_open_count,
        },
        "coverage": {
            "ratio": dashboard.coverage.coverage_ratio,
            "total_area_sqft": dashboard.coverage.total_area_sqft,
            "covered_area_sqft": dashboard.coverage.covered_area_sqft,
            "pending_area_sqft": dashboard.coverage.pending_area_sqft,
            "method": dashboard.coverage.method,
        },
        "capture_health": dashboard.capture_health.model_dump(),
        "quality": dashboard.quality.model_dump(),
        "frequency": {
            "expected_per_week": dashboard.frequency.expected_per_week,
            "last_capture_at": _iso(dashboard.frequency.last_capture_at),
            "days_since_last_capture": dashboard.frequency.days_since_last_capture,
            "next_expected_date": _iso(dashboard.frequency.next_expected_date),
            "status": str(dashboard.frequency.status),
        },
        "floors": [
            {
                "floor": item.floor_name,
                "level": item.level,
                "coverage_ratio": item.coverage_ratio,
                "locations": item.location_count,
                "visited": item.visited_locations,
                "last_capture_at": _iso(item.last_capture_at),
            }
            for item in dashboard.floors
        ],
        "timeline": [
            {
                "date": entry.capture_date.isoformat(),
                "sessions": entry.session_count,
                "frames": entry.frame_count,
                "coverage_ratio": entry.coverage_ratio,
                "floors": entry.floors,
            }
            for entry in entries
        ],
        "attention_required": {
            "never_captured_locations": len(stale.never_captured),
            "stale_locations": len(stale.stale),
            "stale_threshold_days": stale.threshold_days,
            "items": [
                {
                    "code": item.code,
                    "floor": item.floor_name,
                    "zone": item.zone_name,
                    "days_since_capture": item.days_since_capture,
                }
                for item in (stale.stale + stale.never_captured)[:50]
            ],
        },
        "progress_analysis": {
            "available": False,
            "reason": (
                "Progress detection requires a trained model and is a Phase 3 capability. "
                "Coverage and capture frequency above are measured; no progress percentage is "
                "inferred."
            ),
        },
    }


def _floor_coverage(
    db: Session,
    project: Project,
    floor_id: str | None,
    period_start: date | None,
    period_end: date | None,
) -> dict[str, Any]:
    coverage = monitoring.compute_coverage(db, project.id)
    floors = coverage.floors
    if floor_id:
        floors = [item for item in floors if item.floor_id == floor_id]
        if not floors:
            raise NotFoundError("That floor is not part of this project")

    return {
        "title": f"Floor coverage — {project.name}",
        "method": coverage.method,
        "overall_ratio": coverage.coverage_ratio,
        "rows": [
            {
                "floor": item.floor_name,
                "level": item.level,
                "zone": zone.zone_name,
                "area_sqft": zone.area_sqft,
                "expected_points": zone.expected_capture_points,
                "existing_points": zone.actual_capture_points,
                "coverage_ratio": zone.coverage_ratio,
                "covered_area_sqft": zone.covered_area_sqft,
                "pending_area_sqft": zone.pending_area_sqft,
            }
            for item in floors
            for zone in item.zones
        ]
        or [
            # A floor with no declared zones still belongs in the report — as a floor-level row
            # with an explicit note, not as a silent omission.
            {
                "floor": item.floor_name,
                "level": item.level,
                "zone": "(no zones defined)",
                "area_sqft": item.area_sqft,
                "expected_points": item.expected_capture_points,
                "existing_points": item.actual_capture_points,
                "coverage_ratio": item.coverage_ratio,
                "covered_area_sqft": item.covered_area_sqft,
                "pending_area_sqft": item.pending_area_sqft,
            }
            for item in floors
        ],
        "floor_totals": [
            {
                "floor": item.floor_name,
                "level": item.level,
                "coverage_ratio": item.coverage_ratio,
                "expected_points": item.expected_capture_points,
                "visited_locations": item.visited_locations,
                "last_capture_at": _iso(item.last_capture_at),
            }
            for item in floors
        ],
    }


def _capture_history(
    db: Session,
    project: Project,
    floor_id: str | None,
    period_start: date | None,
    period_end: date | None,
) -> dict[str, Any]:
    query = (
        select(CaptureSession, Floor.name, Building.name, Zone.name, User.full_name)
        .join(Floor, Floor.id == CaptureSession.floor_id)
        .join(Building, Building.id == Floor.building_id)
        .outerjoin(Zone, Zone.id == CaptureSession.zone_id)
        .outerjoin(User, User.id == CaptureSession.operator_id)
        .where(CaptureSession.project_id == project.id)
        .order_by(CaptureSession.started_at.desc())
    )
    if floor_id:
        query = query.where(CaptureSession.floor_id == floor_id)
    if period_start:
        query = query.where(func.date(CaptureSession.started_at) >= period_start)
    if period_end:
        query = query.where(func.date(CaptureSession.started_at) <= period_end)

    rows = db.execute(query.limit(1000)).all()
    session_ids = [row[0].id for row in rows]

    mean_quality: dict[str, float] = {}
    thumbnails: dict[str, str] = {}
    placed_counts: dict[str, int] = {}
    if session_ids:
        placed_counts = {
            sid: int(count)
            for sid, count in db.execute(
                select(CaptureFrame.session_id, func.count(CaptureFrame.id))
                .where(
                    CaptureFrame.session_id.in_(session_ids),
                    CaptureFrame.location_id.is_not(None),
                )
                .group_by(CaptureFrame.session_id)
            )
        }
        for sid, mean in db.execute(
            select(CaptureFrame.session_id, func.avg(QualityResult.quality_score))
            .join(QualityResult, QualityResult.frame_id == CaptureFrame.id)
            .where(CaptureFrame.session_id.in_(session_ids))
            .group_by(CaptureFrame.session_id)
        ):
            if mean is not None:
                mean_quality[sid] = round(float(mean), 3)

        for sid, key in db.execute(
            select(CaptureFrame.session_id, func.min(CaptureFrame.thumbnail_key))
            .where(
                CaptureFrame.session_id.in_(session_ids),
                CaptureFrame.thumbnail_key.is_not(None),
            )
            .group_by(CaptureFrame.session_id)
        ):
            url = sign_url(key)
            if url:
                thumbnails[sid] = url

    return {
        "title": f"Capture history — {project.name}",
        "rows": [
            {
                "session_id": session.id,
                "date": session.started_at.date().isoformat(),
                "time": session.started_at.strftime("%H:%M"),
                "building": building_name,
                "floor": floor_name,
                "zone": zone_name,
                "operator": operator_name,
                "camera": session.camera_label,
                "source": str(session.capture_source),
                "frames": session.frame_count,
                "files": session.file_count,
                "status": str(session.status),
                "mapping_confidence": str(session.mapping_confidence),
                # How this capture reached the plan, per row: the provenance block says what
                # happened across the report, and a reader looking at one line should not have
                # to infer which of those applied to it.
                "placement": _describe_placement(session, placed_counts.get(session.id, 0)),
                "mean_quality_score": mean_quality.get(session.id),
                "thumbnail_url": thumbnails.get(session.id),
            }
            for session, floor_name, building_name, zone_name, operator_name in rows
        ],
        "summary": {
            "sessions": len(rows),
            "frames": sum(row[0].frame_count for row in rows),
            "ready": sum(1 for row in rows if row[0].status == CaptureSessionStatus.READY),
            "failed": sum(1 for row in rows if row[0].status == CaptureSessionStatus.FAILED),
        },
    }


def _annotation_log(
    db: Session,
    project: Project,
    floor_id: str | None,
    period_start: date | None,
    period_end: date | None,
) -> dict[str, Any]:
    query = (
        select(
            Annotation, Floor.name, CaptureLocation.code, CaptureFrame.captured_at, User.full_name
        )
        .join(CaptureFrame, CaptureFrame.id == Annotation.frame_id)
        .outerjoin(CaptureLocation, CaptureLocation.id == CaptureFrame.location_id)
        .join(Floor, Floor.id == Annotation.floor_id)
        .outerjoin(User, User.id == Annotation.created_by_id)
        .where(Annotation.project_id == project.id)
        .order_by(Annotation.created_at.desc())
    )
    if floor_id:
        query = query.where(Annotation.floor_id == floor_id)
    if period_start:
        query = query.where(func.date(Annotation.created_at) >= period_start)
    if period_end:
        query = query.where(func.date(Annotation.created_at) <= period_end)

    rows = db.execute(query.limit(2000)).all()
    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for annotation, *_ in rows:
        by_status[str(annotation.status)] = by_status.get(str(annotation.status), 0) + 1
        by_priority[str(annotation.priority)] = by_priority.get(str(annotation.priority), 0) + 1

    return {
        "title": f"Annotation log — {project.name}",
        "rows": [
            {
                "id": annotation.id,
                "created": annotation.created_at.date().isoformat(),
                "floor": floor_name,
                "location": location_code,
                "captured_at": _iso(captured_at),
                "title": annotation.title,
                "description": annotation.description,
                "priority": str(annotation.priority),
                "status": str(annotation.status),
                "raised_by": creator_name,
                "resolved_at": _iso(annotation.resolved_at),
            }
            for annotation, floor_name, location_code, captured_at, creator_name in rows
        ],
        "summary": {
            "total": len(rows),
            "by_status": by_status,
            "by_priority": by_priority,
            "open": sum(1 for row in rows if row[0].status != AnnotationStatus.RESOLVED),
        },
    }


# ---- helpers -----------------------------------------------------------------
def _in_period(value: date, period_start: date | None, period_end: date | None) -> bool:
    if period_start and value < period_start:
        return False
    return not (period_end and value > period_end)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat()
