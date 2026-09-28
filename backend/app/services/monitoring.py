"""Coverage, timeline, capture frequency, dashboards, stale areas (§16, §19, §20, §21).

Every number produced here carries its provenance and its method. Two rules follow from §46 and
are enforced by construction, not by convention:

1. **Coverage is not inferred from imagery.** It is the ratio of capture locations actually
   visited to the capture points an administrator declared for the zone. Square footage is
   apportioned from that ratio, and the response says so in ``method``.
2. **An absent number is absent, not zero.** A floor with no declared zones reports
   ``coverage_ratio`` against locations, and a project with nothing captured reports
   ``is_empty``, so the UI can show an empty state rather than a wall of confident zeroes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.enums import (
    TERMINAL_SESSION_STATUSES,
    AnnotationStatus,
    CaptureFrequencyStatus,
    CaptureSessionStatus,
    DataProvenance,
    QualityVerdict,
)
from app.core.errors import NotFoundError
from app.models import (
    Annotation,
    Building,
    CaptureFrame,
    CaptureLocation,
    CaptureSchedule,
    CaptureSession,
    Floor,
    FloorPlan,
    Project,
    QualityResult,
    User,
    Zone,
)
from app.schemas.monitoring import (
    CaptureHealth,
    CoverageOut,
    FloorCoverage,
    FloorStatus,
    FrequencyStatusOut,
    PortfolioOut,
    PortfolioRow,
    ProjectDashboard,
    QualityBreakdown,
    RecentCapture,
    StaleAreasOut,
    StaleLocation,
    TimelineEntry,
    TimelineOut,
    ZoneCoverage,
)

COVERAGE_METHOD = (
    "Visited capture locations ÷ administrator-declared expected capture points. "
    "Square footage is apportioned from that ratio — it is not measured from imagery."
)
#: A location not revisited within this window counts as stale.
DEFAULT_STALE_DAYS = 14


# ---- shared query fragments --------------------------------------------------
def _floor_ids_for_project(db: Session, project_id: str) -> list[str]:
    return list(
        db.scalars(select(Floor.id).join(Building).where(Building.project_id == project_id))
    )


def _project_sessions(project_id: str) -> Select:
    return select(CaptureSession).where(CaptureSession.project_id == project_id)


def _visited_location_ids(
    db: Session, floor_ids: list[str], since: datetime | None = None
) -> dict[str, set[str]]:
    """``{floor_id: {location_id, …}}`` for locations with at least one frame assigned."""
    if not floor_ids:
        return {}
    query = (
        select(CaptureLocation.floor_id, CaptureLocation.id)
        .join(CaptureFrame, CaptureFrame.location_id == CaptureLocation.id)
        .where(CaptureLocation.floor_id.in_(floor_ids))
    )
    if since is not None:
        query = query.where(CaptureFrame.captured_at >= since)
    result: dict[str, set[str]] = defaultdict(set)
    for floor_id, location_id in db.execute(query.distinct()):
        result[floor_id].add(location_id)
    return result


# ---- coverage ----------------------------------------------------------------
def compute_coverage(
    db: Session, project_id: str, *, recent_days: int | None = None
) -> CoverageOut:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")

    floors = list(
        db.scalars(
            select(Floor)
            .join(Building)
            .where(Building.project_id == project_id)
            .order_by(Floor.level)
        )
    )
    floor_ids = [floor.id for floor in floors]
    since = datetime.now(UTC) - timedelta(days=recent_days) if recent_days else None

    visited_all = _visited_location_ids(db, floor_ids)
    visited_recent = _visited_location_ids(db, floor_ids, since) if since else visited_all

    locations_by_floor: dict[str, list[CaptureLocation]] = defaultdict(list)
    if floor_ids:
        for location in db.scalars(
            select(CaptureLocation).where(CaptureLocation.floor_id.in_(floor_ids))
        ):
            locations_by_floor[location.floor_id].append(location)

    last_capture_by_floor = _last_capture_by_floor(db, floor_ids)

    floor_reports: list[FloorCoverage] = []
    total_expected = 0
    total_visited = 0
    total_area = 0.0
    covered_area = 0.0
    has_area = False

    for floor in floors:
        zones = list(db.scalars(select(Zone).where(Zone.floor_id == floor.id).order_by(Zone.name)))
        floor_locations = locations_by_floor.get(floor.id, [])
        visited_ids = visited_all.get(floor.id, set())
        recent_ids = visited_recent.get(floor.id, set())

        zone_reports: list[ZoneCoverage] = []
        zone_expected_sum = 0
        for zone in zones:
            zone_locations = [loc for loc in floor_locations if loc.zone_id == zone.id]
            zone_visited = sum(1 for loc in zone_locations if loc.id in visited_ids)
            zone_recent = sum(1 for loc in zone_locations if loc.id in recent_ids)
            expected = max(zone.expected_capture_points, 0)
            zone_expected_sum += expected
            ratio = _ratio(zone_visited, expected if expected else len(zone_locations))
            zone_reports.append(
                ZoneCoverage(
                    zone_id=zone.id,
                    zone_name=zone.name,
                    area_sqft=zone.area_sqft,
                    expected_capture_points=expected,
                    actual_capture_points=len(zone_locations),
                    captured_points_recent=zone_recent,
                    coverage_ratio=ratio,
                    covered_area_sqft=(
                        round(zone.area_sqft * ratio, 1) if zone.area_sqft else None
                    ),
                    pending_area_sqft=(
                        round(zone.area_sqft * (1 - ratio), 1) if zone.area_sqft else None
                    ),
                )
            )

        # Denominator preference: declared zone expectations, else the locations that exist.
        # Never a guess, and never zero-divided into a fake 100%.
        expected_points = zone_expected_sum or len(floor_locations)
        visited_count = len(visited_ids)
        floor_ratio = _ratio(visited_count, expected_points)

        floor_area = floor.area_sqft or sum(z.area_sqft or 0.0 for z in zones) or None
        if floor_area:
            has_area = True
            total_area += floor_area
            covered_area += floor_area * floor_ratio

        total_expected += expected_points
        total_visited += min(visited_count, expected_points) if expected_points else 0

        floor_reports.append(
            FloorCoverage(
                floor_id=floor.id,
                floor_name=floor.name,
                level=floor.level,
                area_sqft=floor_area,
                expected_capture_points=expected_points,
                actual_capture_points=len(floor_locations),
                visited_locations=visited_count,
                coverage_ratio=floor_ratio,
                covered_area_sqft=round(floor_area * floor_ratio, 1) if floor_area else None,
                pending_area_sqft=(
                    round(floor_area * (1 - floor_ratio), 1) if floor_area else None
                ),
                last_capture_at=last_capture_by_floor.get(floor.id),
                zones=zone_reports,
            )
        )

    overall = _ratio(total_visited, total_expected)
    return CoverageOut(
        project_id=project_id,
        coverage_ratio=overall,
        total_area_sqft=round(total_area, 1) if has_area else None,
        covered_area_sqft=round(covered_area, 1) if has_area else None,
        pending_area_sqft=round(total_area - covered_area, 1) if has_area else None,
        provenance=DataProvenance.MANUAL,
        method=COVERAGE_METHOD,
        floors=floor_reports,
    )


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(min(numerator / denominator, 1.0), 4)


def _last_capture_by_floor(db: Session, floor_ids: list[str]) -> dict[str, datetime]:
    if not floor_ids:
        return {}
    rows = db.execute(
        select(CaptureSession.floor_id, func.max(CaptureSession.started_at))
        .where(
            CaptureSession.floor_id.in_(floor_ids),
            CaptureSession.status == CaptureSessionStatus.READY,
        )
        .group_by(CaptureSession.floor_id)
    )
    return {floor_id: moment for floor_id, moment in rows if moment is not None}


# ---- timeline ----------------------------------------------------------------
def compute_timeline(
    db: Session,
    project_id: str,
    *,
    floor_id: str | None = None,
    limit: int = 60,
) -> TimelineOut:
    """Capture activity grouped by calendar date (§16)."""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")

    floor_ids = [floor_id] if floor_id else _floor_ids_for_project(db, project_id)
    location_total = 0
    if floor_ids:
        location_total = int(
            db.scalar(
                select(func.count(CaptureLocation.id)).where(
                    CaptureLocation.floor_id.in_(floor_ids)
                )
            )
            or 0
        )

    query = (
        select(CaptureSession, Floor.name, User.full_name)
        .join(Floor, Floor.id == CaptureSession.floor_id)
        .outerjoin(User, User.id == CaptureSession.operator_id)
        .where(CaptureSession.project_id == project_id)
        .order_by(CaptureSession.started_at.desc())
    )
    if floor_id:
        query = query.where(CaptureSession.floor_id == floor_id)

    buckets: dict[date, dict] = {}
    for session, floor_name, operator_name in db.execute(query):
        day = session.started_at.astimezone(UTC).date()
        bucket = buckets.setdefault(
            day,
            {
                "session_ids": [],
                "frame_count": 0,
                "floors": set(),
                "operators": set(),
                "location_ids": set(),
            },
        )
        bucket["session_ids"].append(session.id)
        bucket["frame_count"] += session.frame_count
        bucket["floors"].add(floor_name)
        if operator_name:
            bucket["operators"].add(operator_name)

    if buckets:
        session_ids = [sid for bucket in buckets.values() for sid in bucket["session_ids"]]
        located = db.execute(
            select(CaptureSession.id, CaptureFrame.location_id)
            .join(CaptureFrame, CaptureFrame.session_id == CaptureSession.id)
            .where(
                CaptureSession.id.in_(session_ids),
                CaptureFrame.location_id.is_not(None),
            )
            .distinct()
        )
        session_to_day = {
            sid: day for day, bucket in buckets.items() for sid in bucket["session_ids"]
        }
        for sid, location_id in located:
            day = session_to_day.get(sid)
            if day is not None:
                buckets[day]["location_ids"].add(location_id)

    entries: list[TimelineEntry] = []
    for day in sorted(buckets, reverse=True)[:limit]:
        bucket = buckets[day]
        located_count = len(bucket["location_ids"])
        entries.append(
            TimelineEntry(
                capture_date=day,
                session_ids=bucket["session_ids"],
                session_count=len(bucket["session_ids"]),
                frame_count=bucket["frame_count"],
                located_frame_count=located_count,
                coverage_ratio=_ratio(located_count, location_total),
                coverage_provenance=DataProvenance.MEASURED,
                floors=sorted(name for name in bucket["floors"] if name),
                operators=sorted(bucket["operators"]),
            )
        )

    note = None
    if location_total == 0:
        note = (
            "No capture locations are defined yet, so per-date coverage cannot be calculated. "
            "Add capture points on the floor plan first."
        )
    return TimelineOut(
        project_id=project_id,
        floor_id=floor_id,
        entries=entries,
        location_total=location_total,
        note=note,
    )


# ---- capture frequency -------------------------------------------------------
def compute_frequency(
    db: Session, project_id: str, *, floor_id: str | None = None
) -> FrequencyStatusOut:
    """Last capture, days since, next expected, and due/overdue status (§20)."""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")

    schedule = db.scalar(
        select(CaptureSchedule).where(
            CaptureSchedule.project_id == project_id,
            CaptureSchedule.floor_id == floor_id,
        )
    )
    if schedule is None and floor_id is not None:
        schedule = db.scalar(
            select(CaptureSchedule).where(
                CaptureSchedule.project_id == project_id,
                CaptureSchedule.floor_id.is_(None),
            )
        )

    expected_per_week = (
        schedule.expected_per_week if schedule else project.expected_captures_per_week
    )
    grace_days = schedule.grace_days if schedule else 1

    query = select(func.max(CaptureSession.started_at)).where(
        CaptureSession.project_id == project_id,
        CaptureSession.status == CaptureSessionStatus.READY,
    )
    if floor_id:
        query = query.where(CaptureSession.floor_id == floor_id)
    last_capture = db.scalar(query)

    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    week_query = select(func.count(CaptureSession.id)).where(
        CaptureSession.project_id == project_id,
        CaptureSession.status == CaptureSessionStatus.READY,
        CaptureSession.started_at >= week_ago,
    )
    if floor_id:
        week_query = week_query.where(CaptureSession.floor_id == floor_id)
    captures_this_week = int(db.scalar(week_query) or 0)

    floor_name = None
    if floor_id:
        floor = db.get(Floor, floor_id)
        floor_name = floor.name if floor else None

    if last_capture is None:
        status = CaptureFrequencyStatus.NEVER_CAPTURED
        days_since = None
        next_expected = None
    else:
        days_since = (now - last_capture).days
        # Two per week means one every 3.5 days; that is the interval, not "twice on Monday".
        interval_days = 7.0 / expected_per_week if expected_per_week > 0 else None
        if interval_days is None:
            status = CaptureFrequencyStatus.OK
            next_expected = None
        else:
            next_expected = (last_capture + timedelta(days=interval_days)).date()
            overdue_after = last_capture + timedelta(days=interval_days + grace_days)
            if now > overdue_after:
                status = CaptureFrequencyStatus.OVERDUE
            elif now >= last_capture + timedelta(days=interval_days):
                status = CaptureFrequencyStatus.DUE
            else:
                status = CaptureFrequencyStatus.OK

    return FrequencyStatusOut(
        project_id=project_id,
        project_name=project.name,
        floor_id=floor_id,
        floor_name=floor_name,
        expected_per_week=expected_per_week,
        last_capture_at=last_capture,
        days_since_last_capture=days_since,
        next_expected_date=next_expected,
        status=status,
        captures_this_week=captures_this_week,
    )


# ---- dashboard ---------------------------------------------------------------
def build_dashboard(db: Session, project_id: str) -> ProjectDashboard:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")

    floor_ids = _floor_ids_for_project(db, project_id)
    building_count = int(
        db.scalar(select(func.count(Building.id)).where(Building.project_id == project_id)) or 0
    )
    zone_count = (
        int(db.scalar(select(func.count(Zone.id)).where(Zone.floor_id.in_(floor_ids))) or 0)
        if floor_ids
        else 0
    )
    location_count = (
        int(
            db.scalar(
                select(func.count(CaptureLocation.id)).where(
                    CaptureLocation.floor_id.in_(floor_ids)
                )
            )
            or 0
        )
        if floor_ids
        else 0
    )

    status_rows = db.execute(
        select(CaptureSession.status, func.count(CaptureSession.id))
        .where(CaptureSession.project_id == project_id)
        .group_by(CaptureSession.status)
    )
    counts = {CaptureSessionStatus(status): count for status, count in status_rows}
    session_count = sum(counts.values())
    completed = counts.get(CaptureSessionStatus.READY, 0)
    failed = counts.get(CaptureSessionStatus.FAILED, 0)
    cancelled = counts.get(CaptureSessionStatus.CANCELLED, 0)
    in_progress = sum(
        count
        for status, count in counts.items()
        if status not in TERMINAL_SESSION_STATUSES | {CaptureSessionStatus.DRAFT}
    )
    pending = counts.get(CaptureSessionStatus.DRAFT, 0) + counts.get(
        CaptureSessionStatus.RECORDING, 0
    )

    frame_count = int(
        db.scalar(
            select(func.count(CaptureFrame.id))
            .join(CaptureSession, CaptureSession.id == CaptureFrame.session_id)
            .where(CaptureSession.project_id == project_id)
        )
        or 0
    )

    quality_rows = db.execute(
        select(QualityResult.verdict, func.count(QualityResult.id))
        .join(CaptureFrame, CaptureFrame.id == QualityResult.frame_id)
        .join(CaptureSession, CaptureSession.id == CaptureFrame.session_id)
        .where(CaptureSession.project_id == project_id)
        .group_by(QualityResult.verdict)
    )
    quality_map = {str(verdict): count for verdict, count in quality_rows}
    scored = sum(quality_map.values())
    quality = QualityBreakdown(
        good=quality_map.get(str(QualityVerdict.GOOD), 0),
        warning=quality_map.get(str(QualityVerdict.WARNING), 0),
        poor=quality_map.get(str(QualityVerdict.POOR), 0),
        unusable=quality_map.get(str(QualityVerdict.UNUSABLE), 0),
        unscored=max(frame_count - scored, 0),
    )

    open_annotations = int(
        db.scalar(
            select(func.count(Annotation.id)).where(
                Annotation.project_id == project_id,
                Annotation.status != AnnotationStatus.RESOLVED,
            )
        )
        or 0
    )

    coverage = compute_coverage(db, project_id)
    frequency = compute_frequency(db, project_id)

    floors = [
        FloorStatus(
            floor_id=report.floor_id,
            floor_name=report.floor_name,
            level=report.level,
            coverage_ratio=report.coverage_ratio,
            location_count=report.actual_capture_points,
            visited_locations=report.visited_locations,
            last_capture_at=report.last_capture_at,
        )
        for report in sorted(coverage.floors, key=lambda item: item.level, reverse=True)
    ]

    recent = _recent_captures(db, project_id, limit=10)
    last_capture_at = frequency.last_capture_at

    return ProjectDashboard(
        project_id=project.id,
        project_name=project.name,
        project_code=project.code,
        building_count=building_count,
        floor_count=len(floor_ids),
        zone_count=zone_count,
        location_count=location_count,
        session_count=session_count,
        frame_count=frame_count,
        annotation_open_count=open_annotations,
        last_capture_at=last_capture_at,
        coverage=coverage,
        capture_health=CaptureHealth(
            total_sessions=session_count,
            completed=completed,
            in_progress=in_progress,
            pending=pending,
            failed=failed + cancelled,
        ),
        quality=quality,
        frequency=frequency,
        floors=floors,
        recent_captures=recent,
        is_empty=frame_count == 0 and session_count == 0,
    )


def _recent_captures(db: Session, project_id: str, *, limit: int) -> list[RecentCapture]:
    rows = db.execute(
        select(CaptureSession, Floor.name, Zone.name, User.full_name)
        .join(Floor, Floor.id == CaptureSession.floor_id)
        .outerjoin(Zone, Zone.id == CaptureSession.zone_id)
        .outerjoin(User, User.id == CaptureSession.operator_id)
        .where(CaptureSession.project_id == project_id)
        .order_by(CaptureSession.started_at.desc())
        .limit(limit)
    ).all()
    if not rows:
        return []

    session_ids = [row[0].id for row in rows]
    # Worst verdict wins: one unusable frame in a session is what the site engineer needs to know.
    verdict_rank = {
        str(QualityVerdict.GOOD): 0,
        str(QualityVerdict.WARNING): 1,
        str(QualityVerdict.POOR): 2,
        str(QualityVerdict.UNUSABLE): 3,
    }
    worst: dict[str, str] = {}
    verdict_rows = db.execute(
        select(CaptureFrame.session_id, QualityResult.verdict)
        .join(QualityResult, QualityResult.frame_id == CaptureFrame.id)
        .where(CaptureFrame.session_id.in_(session_ids))
    )
    for sid, verdict in verdict_rows:
        current = worst.get(sid)
        if current is None or verdict_rank.get(str(verdict), 0) > verdict_rank.get(current, 0):
            worst[sid] = str(verdict)

    return [
        RecentCapture(
            session_id=session.id,
            captured_at=session.started_at,
            operator_name=operator_name,
            floor_name=floor_name,
            zone_name=zone_name,
            frame_count=session.frame_count,
            quality_verdict=QualityVerdict(worst[session.id]) if session.id in worst else None,
            status=session.status,
            mapping_confidence=session.mapping_confidence,
        )
        for session, floor_name, zone_name, operator_name in rows
    ]


# ---- portfolio ---------------------------------------------------------------
def build_portfolio(
    db: Session, *, limit: int = 100, project_ids: list[str] | None = None
) -> PortfolioOut:
    """Cross-project overview for the top-level dashboard.

    ``project_ids`` restricts it to the caller's projects; ``None`` means no restriction, which is
    what an administrator gets. Passed in rather than resolved here so this service keeps knowing
    nothing about authentication — the route decides who is asking, and this counts what it is
    told to count.
    """
    query = select(Project)
    if project_ids is not None:
        query = query.where(Project.id.in_(project_ids))
    projects = list(db.scalars(query.order_by(Project.name).limit(limit)))
    rows: list[PortfolioRow] = []
    overdue = 0
    total_sessions = 0
    total_frames = 0

    for project in projects:
        coverage = compute_coverage(db, project.id)
        frequency = compute_frequency(db, project.id)
        floor_ids = _floor_ids_for_project(db, project.id)
        session_count = int(
            db.scalar(
                select(func.count(CaptureSession.id)).where(CaptureSession.project_id == project.id)
            )
            or 0
        )
        frame_count = int(
            db.scalar(
                select(func.count(CaptureFrame.id))
                .join(CaptureSession, CaptureSession.id == CaptureFrame.session_id)
                .where(CaptureSession.project_id == project.id)
            )
            or 0
        )
        open_annotations = int(
            db.scalar(
                select(func.count(Annotation.id)).where(
                    Annotation.project_id == project.id,
                    Annotation.status != AnnotationStatus.RESOLVED,
                )
            )
            or 0
        )
        if frequency.status is CaptureFrequencyStatus.OVERDUE:
            overdue += 1
        total_sessions += session_count
        total_frames += frame_count

        rows.append(
            PortfolioRow(
                project_id=project.id,
                project_name=project.name,
                project_code=project.code,
                status=str(project.status),
                floor_count=len(floor_ids),
                session_count=session_count,
                coverage_ratio=coverage.coverage_ratio,
                last_capture_at=frequency.last_capture_at,
                frequency_status=frequency.status,
                open_annotations=open_annotations,
            )
        )

    return PortfolioOut(
        projects=rows,
        total_projects=len(rows),
        total_sessions=total_sessions,
        total_frames=total_frames,
        overdue_count=overdue,
    )


# ---- stale areas -------------------------------------------------------------
def find_stale_areas(
    db: Session, project_id: str, *, threshold_days: int = DEFAULT_STALE_DAYS
) -> StaleAreasOut:
    """Locations never captured, or not captured recently (product vision item 11)."""
    if db.get(Project, project_id) is None:
        raise NotFoundError(f"Project {project_id} not found")

    floor_ids = _floor_ids_for_project(db, project_id)
    if not floor_ids:
        return StaleAreasOut(
            project_id=project_id, threshold_days=threshold_days, never_captured=[], stale=[]
        )

    rows = db.execute(
        select(
            CaptureLocation,
            Floor.name,
            Zone.name,
            func.max(CaptureFrame.captured_at),
        )
        .join(Floor, Floor.id == CaptureLocation.floor_id)
        .outerjoin(Zone, Zone.id == CaptureLocation.zone_id)
        .outerjoin(CaptureFrame, CaptureFrame.location_id == CaptureLocation.id)
        .where(CaptureLocation.floor_id.in_(floor_ids))
        .group_by(CaptureLocation.id, Floor.name, Zone.name)
        .order_by(Floor.name, CaptureLocation.code)
    ).all()

    now = datetime.now(UTC)
    never: list[StaleLocation] = []
    stale: list[StaleLocation] = []

    for location, floor_name, zone_name, last_captured in rows:
        if last_captured is None:
            never.append(
                StaleLocation(
                    location_id=location.id,
                    code=location.code,
                    label=location.label,
                    floor_id=location.floor_id,
                    floor_name=floor_name,
                    zone_name=zone_name,
                    last_captured_at=None,
                    days_since_capture=None,
                )
            )
            continue
        if last_captured.tzinfo is None:
            last_captured = last_captured.replace(tzinfo=UTC)
        days = (now - last_captured).days
        if days >= threshold_days:
            stale.append(
                StaleLocation(
                    location_id=location.id,
                    code=location.code,
                    label=location.label,
                    floor_id=location.floor_id,
                    floor_name=floor_name,
                    zone_name=zone_name,
                    last_captured_at=last_captured,
                    days_since_capture=days,
                )
            )

    stale.sort(key=lambda item: item.days_since_capture or 0, reverse=True)
    return StaleAreasOut(
        project_id=project_id,
        threshold_days=threshold_days,
        never_captured=never,
        stale=stale,
    )


def active_floor_plan(db: Session, floor_id: str) -> FloorPlan | None:
    return db.scalar(
        select(FloorPlan)
        .where(FloorPlan.floor_id == floor_id, FloorPlan.is_active.is_(True))
        .order_by(FloorPlan.created_at.desc())
        .limit(1)
    )
