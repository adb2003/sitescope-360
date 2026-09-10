"""Project hierarchy routes: projects, buildings, floors, zones (§22, §26)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import func, select

from app.api.deps import (
    DbSession,
    Pagination,
    RequireProjectDelete,
    RequireProjectRead,
    RequireProjectSettings,
    RequireProjectWrite,
    get_building_or_404,
    get_floor_or_404,
    get_project_or_404,
    project_id_for_floor,
)
from app.core.enums import ProjectStatus
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import log_event
from app.models import (
    Building,
    CaptureFrame,
    CaptureLocation,
    CaptureSchedule,
    CaptureSession,
    Floor,
    FloorPlan,
    Project,
    Zone,
)
from app.schemas.common import Message, Page
from app.schemas.project import (
    BuildingCreate,
    BuildingOut,
    BuildingUpdate,
    CaptureScheduleIn,
    CaptureScheduleOut,
    FloorCreate,
    FloorDetail,
    FloorOut,
    FloorUpdate,
    ProjectCreate,
    ProjectOut,
    ProjectSummary,
    ProjectUpdate,
    ZoneCreate,
    ZoneOut,
    ZoneUpdate,
)

router = APIRouter(tags=["projects"])


# ---- projects ----------------------------------------------------------------
@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: DbSession, user: RequireProjectWrite) -> ProjectOut:
    code = payload.code.strip()
    if db.scalar(select(Project.id).where(Project.code == code)):
        raise ConflictError(f"A project with the code {code!r} already exists")
    if (
        payload.start_date
        and payload.expected_completion_date
        and payload.expected_completion_date < payload.start_date
    ):
        raise ValidationError("Expected completion date cannot be before the start date")

    project = Project(
        **payload.model_dump(exclude={"code"}),
        code=code,
        created_by_id=user.id,
    )
    db.add(project)
    db.commit()
    log_event("project.created", project_id=project.id, code=project.code, user_id=user.id)
    return ProjectOut.model_validate(project)


@router.get("/projects", response_model=Page[ProjectSummary])
def list_projects(
    db: DbSession,
    page: Pagination,
    _: RequireProjectRead,
    q: Annotated[str | None, Query(description="Search name, code or client")] = None,
    project_status: Annotated[ProjectStatus | None, Query(alias="status")] = None,
) -> Page[ProjectSummary]:
    query = select(Project)
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(
            Project.name.ilike(pattern)
            | Project.code.ilike(pattern)
            | Project.client.ilike(pattern)
        )
    if project_status:
        query = query.where(Project.status == project_status)

    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    projects = list(db.scalars(query.order_by(Project.name).limit(page.limit).offset(page.offset)))
    return Page(
        items=[_summarize(db, project) for project in projects],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/projects/{project_id}", response_model=ProjectSummary)
def get_project(project_id: str, db: DbSession, _: RequireProjectRead) -> ProjectSummary:
    return _summarize(db, get_project_or_404(db, project_id))


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: str, payload: ProjectUpdate, db: DbSession, _: RequireProjectWrite
) -> ProjectOut:
    project = get_project_or_404(db, project_id)
    updates = payload.model_dump(exclude_unset=True)

    start = updates.get("start_date", project.start_date)
    end = updates.get("expected_completion_date", project.expected_completion_date)
    if start and end and end < start:
        raise ValidationError("Expected completion date cannot be before the start date")

    for field, value in updates.items():
        setattr(project, field, value)
    db.commit()
    return ProjectOut.model_validate(project)


@router.delete("/projects/{project_id}", response_model=Message)
def delete_project(
    project_id: str,
    db: DbSession,
    _: RequireProjectDelete,
    confirm_code: Annotated[
        str, Query(description="Repeat the project code to confirm this destructive action")
    ],
) -> Message:
    """Deleting a project cascades to every capture, frame and annotation under it.

    The project code must be repeated in the query string. That is not ceremony: this endpoint
    destroys an entire site's capture history, and a mistyped id should not be enough to do it.
    """
    project = get_project_or_404(db, project_id)
    if confirm_code.strip() != project.code:
        raise ValidationError(
            "confirm_code does not match this project's code",
            expected=project.code,
        )

    # Stored objects are removed by prefix. Metadata cascades via foreign keys.
    from app.storage import build_key, get_storage

    try:
        removed = get_storage().delete_prefix(build_key("projects", project.id))
    except Exception:
        removed = -1

    db.delete(project)
    db.commit()
    log_event("project.deleted", project_id=project_id, objects_removed=removed)
    return Message(message=f"Project {project.code} deleted")


def _summarize(db: DbSession, project: Project) -> ProjectSummary:
    floor_ids = list(
        db.scalars(select(Floor.id).join(Building).where(Building.project_id == project.id))
    )
    building_count = int(
        db.scalar(select(func.count(Building.id)).where(Building.project_id == project.id)) or 0
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
    last_capture = db.scalar(
        select(func.max(CaptureSession.started_at)).where(CaptureSession.project_id == project.id)
    )

    return ProjectSummary(
        **ProjectOut.model_validate(project).model_dump(),
        building_count=building_count,
        floor_count=len(floor_ids),
        zone_count=zone_count,
        capture_session_count=session_count,
        frame_count=frame_count,
        location_count=location_count,
        last_capture_at=last_capture,
    )


# ---- buildings ---------------------------------------------------------------
@router.post(
    "/projects/{project_id}/buildings",
    response_model=BuildingOut,
    status_code=status.HTTP_201_CREATED,
)
def create_building(
    project_id: str, payload: BuildingCreate, db: DbSession, _: RequireProjectWrite
) -> BuildingOut:
    project = get_project_or_404(db, project_id)
    if db.scalar(
        select(Building.id).where(Building.project_id == project.id, Building.name == payload.name)
    ):
        raise ConflictError(f"{project.name} already has a building named {payload.name!r}")
    building = Building(project_id=project.id, **payload.model_dump())
    db.add(building)
    db.commit()
    return BuildingOut.model_validate(building)


@router.get("/projects/{project_id}/buildings", response_model=list[BuildingOut])
def list_buildings(project_id: str, db: DbSession, _: RequireProjectRead) -> list[BuildingOut]:
    get_project_or_404(db, project_id)
    rows = db.scalars(
        select(Building).where(Building.project_id == project_id).order_by(Building.name)
    )
    return [BuildingOut.model_validate(row) for row in rows]


@router.get("/buildings/{building_id}", response_model=BuildingOut)
def get_building(building_id: str, db: DbSession, _: RequireProjectRead) -> BuildingOut:
    return BuildingOut.model_validate(get_building_or_404(db, building_id))


@router.patch("/buildings/{building_id}", response_model=BuildingOut)
def update_building(
    building_id: str, payload: BuildingUpdate, db: DbSession, _: RequireProjectWrite
) -> BuildingOut:
    building = get_building_or_404(db, building_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(building, field, value)
    db.commit()
    return BuildingOut.model_validate(building)


@router.delete("/buildings/{building_id}", response_model=Message)
def delete_building(building_id: str, db: DbSession, _: RequireProjectDelete) -> Message:
    building = get_building_or_404(db, building_id)
    db.delete(building)
    db.commit()
    return Message(message=f"Building {building.name} deleted")


# ---- floors ------------------------------------------------------------------
@router.post(
    "/buildings/{building_id}/floors",
    response_model=FloorOut,
    status_code=status.HTTP_201_CREATED,
)
def create_floor(
    building_id: str, payload: FloorCreate, db: DbSession, _: RequireProjectWrite
) -> FloorOut:
    building = get_building_or_404(db, building_id)
    if db.scalar(
        select(Floor.id).where(Floor.building_id == building.id, Floor.name == payload.name)
    ):
        raise ConflictError(f"{building.name} already has a floor named {payload.name!r}")
    floor = Floor(building_id=building.id, **payload.model_dump())
    db.add(floor)
    db.commit()
    log_event("floor.created", floor_id=floor.id, building_id=building.id)
    return FloorOut.model_validate(floor)


@router.get("/buildings/{building_id}/floors", response_model=list[FloorOut])
def list_floors(building_id: str, db: DbSession, _: RequireProjectRead) -> list[FloorOut]:
    get_building_or_404(db, building_id)
    rows = db.scalars(select(Floor).where(Floor.building_id == building_id).order_by(Floor.level))
    return [FloorOut.model_validate(row) for row in rows]


@router.get("/projects/{project_id}/floors", response_model=list[FloorDetail])
def list_project_floors(project_id: str, db: DbSession, _: RequireProjectRead) -> list[FloorDetail]:
    """Every floor in a project with its roll-ups — what the floor-plan picker needs."""
    get_project_or_404(db, project_id)
    rows = db.execute(
        select(Floor, Building.name)
        .join(Building, Building.id == Floor.building_id)
        .where(Building.project_id == project_id)
        .order_by(Building.name, Floor.level)
    ).all()
    return [_floor_detail(db, floor, project_id, building_name) for floor, building_name in rows]


@router.get("/floors/{floor_id}", response_model=FloorDetail)
def get_floor(floor_id: str, db: DbSession, _: RequireProjectRead) -> FloorDetail:
    floor = get_floor_or_404(db, floor_id)
    building = get_building_or_404(db, floor.building_id)
    return _floor_detail(db, floor, building.project_id, building.name)


@router.patch("/floors/{floor_id}", response_model=FloorOut)
def update_floor(
    floor_id: str, payload: FloorUpdate, db: DbSession, _: RequireProjectWrite
) -> FloorOut:
    floor = get_floor_or_404(db, floor_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(floor, field, value)
    db.commit()
    return FloorOut.model_validate(floor)


@router.delete("/floors/{floor_id}", response_model=Message)
def delete_floor(floor_id: str, db: DbSession, _: RequireProjectDelete) -> Message:
    floor = get_floor_or_404(db, floor_id)
    db.delete(floor)
    db.commit()
    return Message(message=f"Floor {floor.name} deleted")


def _floor_detail(db: DbSession, floor: Floor, project_id: str, building_name: str) -> FloorDetail:
    zone_count = int(db.scalar(select(func.count(Zone.id)).where(Zone.floor_id == floor.id)) or 0)
    location_count = int(
        db.scalar(
            select(func.count(CaptureLocation.id)).where(CaptureLocation.floor_id == floor.id)
        )
        or 0
    )
    session_count = int(
        db.scalar(select(func.count(CaptureSession.id)).where(CaptureSession.floor_id == floor.id))
        or 0
    )
    last_capture = db.scalar(
        select(func.max(CaptureSession.started_at)).where(CaptureSession.floor_id == floor.id)
    )
    active_plan = db.scalar(
        select(FloorPlan.id)
        .where(FloorPlan.floor_id == floor.id, FloorPlan.is_active.is_(True))
        .order_by(FloorPlan.created_at.desc())
        .limit(1)
    )
    return FloorDetail(
        **FloorOut.model_validate(floor).model_dump(),
        project_id=project_id,
        building_name=building_name,
        zone_count=zone_count,
        location_count=location_count,
        capture_session_count=session_count,
        active_floor_plan_id=active_plan,
        last_capture_at=last_capture,
    )


# ---- zones -------------------------------------------------------------------
@router.post(
    "/floors/{floor_id}/zones", response_model=ZoneOut, status_code=status.HTTP_201_CREATED
)
def create_zone(
    floor_id: str, payload: ZoneCreate, db: DbSession, _: RequireProjectWrite
) -> ZoneOut:
    floor = get_floor_or_404(db, floor_id)
    if db.scalar(select(Zone.id).where(Zone.floor_id == floor.id, Zone.name == payload.name)):
        raise ConflictError(f"{floor.name} already has a zone named {payload.name!r}")
    zone = Zone(floor_id=floor.id, **payload.model_dump())
    db.add(zone)
    db.commit()
    return ZoneOut.model_validate(zone)


@router.get("/floors/{floor_id}/zones", response_model=list[ZoneOut])
def list_zones(floor_id: str, db: DbSession, _: RequireProjectRead) -> list[ZoneOut]:
    get_floor_or_404(db, floor_id)
    rows = db.scalars(select(Zone).where(Zone.floor_id == floor_id).order_by(Zone.name))
    return [ZoneOut.model_validate(row) for row in rows]


@router.patch("/zones/{zone_id}", response_model=ZoneOut)
def update_zone(
    zone_id: str, payload: ZoneUpdate, db: DbSession, _: RequireProjectWrite
) -> ZoneOut:
    zone = db.get(Zone, zone_id)
    if zone is None:
        raise NotFoundError(f"Zone {zone_id} not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(zone, field, value)
    db.commit()
    return ZoneOut.model_validate(zone)


@router.delete("/zones/{zone_id}", response_model=Message)
def delete_zone(zone_id: str, db: DbSession, _: RequireProjectDelete) -> Message:
    zone = db.get(Zone, zone_id)
    if zone is None:
        raise NotFoundError(f"Zone {zone_id} not found")
    db.delete(zone)
    db.commit()
    return Message(message=f"Zone {zone.name} deleted")


# ---- capture schedule (§20) --------------------------------------------------
@router.put("/projects/{project_id}/schedule", response_model=CaptureScheduleOut)
def upsert_schedule(
    project_id: str, payload: CaptureScheduleIn, db: DbSession, _: RequireProjectSettings
) -> CaptureScheduleOut:
    project = get_project_or_404(db, project_id)
    if payload.floor_id and project_id_for_floor(db, payload.floor_id) != project.id:
        raise ValidationError("That floor does not belong to this project")

    schedule = db.scalar(
        select(CaptureSchedule).where(
            CaptureSchedule.project_id == project.id,
            CaptureSchedule.floor_id == payload.floor_id,
        )
    )
    if schedule is None:
        schedule = CaptureSchedule(project_id=project.id, floor_id=payload.floor_id)
        db.add(schedule)
    schedule.expected_per_week = payload.expected_per_week
    schedule.grace_days = payload.grace_days
    schedule.notes = payload.notes

    # Keep the project-level default in step, since the dashboard reads it directly.
    if payload.floor_id is None:
        project.expected_captures_per_week = payload.expected_per_week
    db.commit()
    return CaptureScheduleOut.model_validate(schedule)


@router.get("/projects/{project_id}/schedule", response_model=list[CaptureScheduleOut])
def list_schedules(
    project_id: str, db: DbSession, _: RequireProjectRead
) -> list[CaptureScheduleOut]:
    get_project_or_404(db, project_id)
    rows = db.scalars(select(CaptureSchedule).where(CaptureSchedule.project_id == project_id))
    return [CaptureScheduleOut.model_validate(row) for row in rows]
