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
    get_zone_or_404,
    project_id_for_floor,
)
from app.auth.membership import (
    capped,
    effective_role,
    grant,
    membership_of,
    project_roles_for,
    restrict_to_member_projects,
)
from app.auth.rbac import Capability, capabilities_for
from app.core.enums import ROLE_RANK, ProjectStatus, Role
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
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
    ProjectMember,
    User,
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
    ProjectMemberAdd,
    ProjectMemberOut,
    ProjectMemberRoleUpdate,
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
    db.flush()
    # The creator becomes a member of what they just created. Without this the project vanishes
    # from their own list the moment it is saved — membership is what makes it visible, and a
    # project manager creating a job for their team would be the first person shut out of it.
    #
    # With their own account role, which is the only honest answer: they have just demonstrated
    # `project:write`, and giving them less here would mean an administrator had to grant the
    # creator access to their own new job before they could add a building to it.
    grant(db, project.id, user.id, role=user.role, added_by_id=user.id)
    if project.project_manager_id and project.project_manager_id != user.id:
        # Naming someone as the manager is a statement that they are on this job, and of what
        # they are on it. Capped at their account role by `effective_role`, so this cannot be a
        # way to promote a viewer by writing their id into a field.
        grant(
            db,
            project.id,
            project.project_manager_id,
            role=Role.PROJECT_MANAGER,
            added_by_id=user.id,
        )
    db.commit()
    log_event("project.created", project_id=project.id, code=project.code, user_id=user.id)
    return ProjectOut.model_validate(project)


@router.get("/projects", response_model=Page[ProjectSummary])
def list_projects(
    db: DbSession,
    page: Pagination,
    user: RequireProjectRead,
    q: Annotated[str | None, Query(description="Search name, code or client")] = None,
    project_status: Annotated[ProjectStatus | None, Query(alias="status")] = None,
) -> Page[ProjectSummary]:
    # Narrowed to the caller's projects (§27). A list endpoint resolves no single resource, so
    # the membership check in `deps` has nothing to hook into and the filter has to be here,
    # where it is visible in the query rather than implied.
    query = restrict_to_member_projects(select(Project), Project.id, db, user)
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
    # One query for the whole page's roles, not one per row.
    roles = project_roles_for(db, user, [project.id for project in projects])
    return Page(
        items=[_summarize(db, project, roles.get(project.id)) for project in projects],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/projects/{project_id}", response_model=ProjectSummary)
def get_project(project_id: str, db: DbSession, user: RequireProjectRead) -> ProjectSummary:
    project = get_project_or_404(db, project_id, user)
    return _summarize(db, project, effective_role(db, user, project.id))


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: str, payload: ProjectUpdate, db: DbSession, user: RequireProjectWrite
) -> ProjectOut:
    project = get_project_or_404(db, project_id, user, needs=Capability.PROJECT_WRITE)
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
    user: RequireProjectDelete,
    confirm_code: Annotated[
        str, Query(description="Repeat the project code to confirm this destructive action")
    ],
) -> Message:
    """Deleting a project cascades to every capture, frame and annotation under it.

    The project code must be repeated in the query string. That is not ceremony: this endpoint
    destroys an entire site's capture history, and a mistyped id should not be enough to do it.
    """
    project = get_project_or_404(db, project_id, user, needs=Capability.PROJECT_DELETE)
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


def _summarize(db: DbSession, project: Project, role: Role | None = None) -> ProjectSummary:
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
        your_role=role,
        your_capabilities=sorted(str(name) for name in capabilities_for(role)) if role else [],
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
    project_id: str, payload: BuildingCreate, db: DbSession, user: RequireProjectWrite
) -> BuildingOut:
    project = get_project_or_404(db, project_id, user, needs=Capability.PROJECT_WRITE)
    if db.scalar(
        select(Building.id).where(Building.project_id == project.id, Building.name == payload.name)
    ):
        raise ConflictError(f"{project.name} already has a building named {payload.name!r}")
    building = Building(project_id=project.id, **payload.model_dump())
    db.add(building)
    db.commit()
    return BuildingOut.model_validate(building)


@router.get("/projects/{project_id}/buildings", response_model=list[BuildingOut])
def list_buildings(project_id: str, db: DbSession, user: RequireProjectRead) -> list[BuildingOut]:
    get_project_or_404(db, project_id, user)
    rows = db.scalars(
        select(Building).where(Building.project_id == project_id).order_by(Building.name)
    )
    return [BuildingOut.model_validate(row) for row in rows]


@router.get("/buildings/{building_id}", response_model=BuildingOut)
def get_building(building_id: str, db: DbSession, user: RequireProjectRead) -> BuildingOut:
    return BuildingOut.model_validate(get_building_or_404(db, building_id, user))


@router.patch("/buildings/{building_id}", response_model=BuildingOut)
def update_building(
    building_id: str, payload: BuildingUpdate, db: DbSession, user: RequireProjectWrite
) -> BuildingOut:
    building = get_building_or_404(db, building_id, user, needs=Capability.PROJECT_WRITE)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(building, field, value)
    db.commit()
    return BuildingOut.model_validate(building)


@router.delete("/buildings/{building_id}", response_model=Message)
def delete_building(building_id: str, db: DbSession, user: RequireProjectDelete) -> Message:
    building = get_building_or_404(db, building_id, user, needs=Capability.PROJECT_DELETE)
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
    building_id: str, payload: FloorCreate, db: DbSession, user: RequireProjectWrite
) -> FloorOut:
    building = get_building_or_404(db, building_id, user, needs=Capability.PROJECT_WRITE)
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
def list_floors(building_id: str, db: DbSession, user: RequireProjectRead) -> list[FloorOut]:
    get_building_or_404(db, building_id, user)
    rows = db.scalars(select(Floor).where(Floor.building_id == building_id).order_by(Floor.level))
    return [FloorOut.model_validate(row) for row in rows]


@router.get("/projects/{project_id}/floors", response_model=list[FloorDetail])
def list_project_floors(
    project_id: str, db: DbSession, user: RequireProjectRead
) -> list[FloorDetail]:
    """Every floor in a project with its roll-ups — what the floor-plan picker needs."""
    get_project_or_404(db, project_id, user)
    rows = db.execute(
        select(Floor, Building.name)
        .join(Building, Building.id == Floor.building_id)
        .where(Building.project_id == project_id)
        .order_by(Building.name, Floor.level)
    ).all()
    return [_floor_detail(db, floor, project_id, building_name) for floor, building_name in rows]


@router.get("/floors/{floor_id}", response_model=FloorDetail)
def get_floor(floor_id: str, db: DbSession, user: RequireProjectRead) -> FloorDetail:
    floor = get_floor_or_404(db, floor_id, user)
    building = get_building_or_404(db, floor.building_id, user)
    return _floor_detail(db, floor, building.project_id, building.name)


@router.patch("/floors/{floor_id}", response_model=FloorOut)
def update_floor(
    floor_id: str, payload: FloorUpdate, db: DbSession, user: RequireProjectWrite
) -> FloorOut:
    floor = get_floor_or_404(db, floor_id, user, needs=Capability.PROJECT_WRITE)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(floor, field, value)
    db.commit()
    return FloorOut.model_validate(floor)


@router.delete("/floors/{floor_id}", response_model=Message)
def delete_floor(floor_id: str, db: DbSession, user: RequireProjectDelete) -> Message:
    floor = get_floor_or_404(db, floor_id, user, needs=Capability.PROJECT_DELETE)
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
    floor_id: str, payload: ZoneCreate, db: DbSession, user: RequireProjectWrite
) -> ZoneOut:
    floor = get_floor_or_404(db, floor_id, user, needs=Capability.PROJECT_WRITE)
    if db.scalar(select(Zone.id).where(Zone.floor_id == floor.id, Zone.name == payload.name)):
        raise ConflictError(f"{floor.name} already has a zone named {payload.name!r}")
    zone = Zone(floor_id=floor.id, **payload.model_dump())
    db.add(zone)
    db.commit()
    return ZoneOut.model_validate(zone)


@router.get("/floors/{floor_id}/zones", response_model=list[ZoneOut])
def list_zones(floor_id: str, db: DbSession, user: RequireProjectRead) -> list[ZoneOut]:
    get_floor_or_404(db, floor_id, user)
    rows = db.scalars(select(Zone).where(Zone.floor_id == floor_id).order_by(Zone.name))
    return [ZoneOut.model_validate(row) for row in rows]


@router.patch("/zones/{zone_id}", response_model=ZoneOut)
def update_zone(
    zone_id: str, payload: ZoneUpdate, db: DbSession, user: RequireProjectWrite
) -> ZoneOut:
    zone = get_zone_or_404(db, zone_id, user, needs=Capability.PROJECT_WRITE)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(zone, field, value)
    db.commit()
    return ZoneOut.model_validate(zone)


@router.delete("/zones/{zone_id}", response_model=Message)
def delete_zone(zone_id: str, db: DbSession, user: RequireProjectDelete) -> Message:
    zone = get_zone_or_404(db, zone_id, user, needs=Capability.PROJECT_DELETE)
    db.delete(zone)
    db.commit()
    return Message(message=f"Zone {zone.name} deleted")


# ---- capture schedule (§20) --------------------------------------------------
@router.put("/projects/{project_id}/schedule", response_model=CaptureScheduleOut)
def upsert_schedule(
    project_id: str, payload: CaptureScheduleIn, db: DbSession, user: RequireProjectSettings
) -> CaptureScheduleOut:
    project = get_project_or_404(db, project_id, user, needs=Capability.SETTINGS_PROJECT)
    if payload.floor_id and project_id_for_floor(db, payload.floor_id, user) != project.id:
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
    project_id: str, db: DbSession, user: RequireProjectRead
) -> list[CaptureScheduleOut]:
    get_project_or_404(db, project_id, user)
    rows = db.scalars(select(CaptureSchedule).where(CaptureSchedule.project_id == project_id))
    return [CaptureScheduleOut.model_validate(row) for row in rows]


# ---- membership --------------------------------------------------------------
def _member_out(member: ProjectMember, account: User) -> ProjectMemberOut:
    """One member, reported with both roles and the one that will actually be enforced."""
    return ProjectMemberOut(
        user_id=account.id,
        email=account.email,
        full_name=account.full_name,
        role=member.role,
        account_role=account.role,
        effective_role=capped(account.role, member.role),
        is_active=account.is_active,
        added_at=member.created_at,
        added_by_id=member.added_by_id,
    )


def _assert_may_grant(actor_role: Role, granting: Role) -> None:
    """Nobody hands out a role above the one they hold on this project.

    Without this, `settings:project` is a promotion to anything: a project manager could make
    somebody an administrator on their own job, and then ask that person for whatever they
    wanted. The actor's own role is already capped by their account, so this is a ceiling under
    a ceiling rather than a second opinion about the same thing.
    """
    if ROLE_RANK[granting] > ROLE_RANK[actor_role]:
        raise PermissionDeniedError(
            f"You cannot give somebody the {granting} role, which is above your own "
            f"({actor_role}) on this project",
            your_role=str(actor_role),
            requested_role=str(granting),
        )


@router.get("/projects/{project_id}/members", response_model=list[ProjectMemberOut])
def list_project_members(
    project_id: str, db: DbSession, user: RequireProjectRead
) -> list[ProjectMemberOut]:
    """Who is on this project, and what each of them is **here** (§27).

    Readable by any member, not only by whoever can change it: a site engineer wondering why a
    colleague cannot upload should be able to answer that without an administrator, and the list
    is people already working together on it.

    Three roles per row, which is one more than it sounds: what they are here, the account role
    capping it, and the `effective_role` that will be enforced. They differ in the ordinary case
    — somebody put on a job as a manager whose account was later lowered — and a list showing
    only the stored value would be describing a permission that no longer applies.
    """
    get_project_or_404(db, project_id, user)
    rows = db.execute(
        select(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(User.full_name)
    ).all()
    return [_member_out(member, account) for member, account in rows]


@router.post(
    "/projects/{project_id}/members",
    response_model=ProjectMemberOut,
    status_code=status.HTTP_201_CREATED,
)
def add_project_member(
    project_id: str,
    payload: ProjectMemberAdd,
    db: DbSession,
    user: RequireProjectSettings,
) -> ProjectMemberOut:
    """Give someone access to this project, as something in particular.

    Gated on `settings:project` **held on this project**, which `get_project_or_404` now decides
    from the actor's role here rather than from their account. A project manager can staff their
    own jobs without an administrator, and cannot staff somebody else's.

    The role is required and bounded twice over: `_assert_may_grant` refuses a role above the
    actor's own, and `effective_role` caps whatever is stored at the recipient's account role
    when it is read. The first stops a manager minting an administrator; the second means the
    stored value can never outrank the account even if it is written by some other path.
    """
    get_project_or_404(db, project_id, user, needs=Capability.SETTINGS_PROJECT)
    actor_role = effective_role(db, user, project_id)
    assert actor_role is not None  # the resolver above refuses a non-member
    if payload.user_id:
        account = db.get(User, payload.user_id)
    else:
        account = db.scalar(select(User).where(User.email == str(payload.email)))
    if account is None:
        raise NotFoundError("No such user")
    _assert_may_grant(actor_role, payload.role)

    created = grant(db, project_id, account.id, role=payload.role, added_by_id=user.id)
    db.commit()
    if created:
        log_event(
            "project.member_added",
            project_id=project_id,
            member_id=account.id,
            role=str(payload.role),
            added_by=user.id,
        )
    member = membership_of(db, account.id, project_id)
    assert member is not None  # just granted, or already present
    return _member_out(member, account)


@router.patch(
    "/projects/{project_id}/members/{user_id}",
    response_model=ProjectMemberOut,
)
def set_project_member_role(
    project_id: str,
    user_id: str,
    payload: ProjectMemberRoleUpdate,
    db: DbSession,
    user: RequireProjectSettings,
) -> ProjectMemberOut:
    """Change what somebody is on this project.

    The route that makes per-project roles usable rather than a thing you set once: a viewer
    promoted for the fit-out and put back afterwards should not have to be removed and re-added,
    which would lose who added them and when.

    Nobody can hand out a role above their own here — including their own. An administrator is
    exempt from membership entirely, so this is the project manager's limit, and it is what stops
    `settings:project` being a path to `settings:system`.
    """
    get_project_or_404(db, project_id, user, needs=Capability.SETTINGS_PROJECT)
    actor_role = effective_role(db, user, project_id)
    assert actor_role is not None  # the resolver above refuses a non-member
    member = membership_of(db, user_id, project_id)
    if member is None:
        raise NotFoundError("That user is not a member of this project")
    _assert_may_grant(actor_role, payload.role)

    previous = member.role
    member.role = payload.role
    db.commit()
    if previous != payload.role:
        log_event(
            "project.member_role_changed",
            project_id=project_id,
            member_id=user_id,
            previous_role=str(previous),
            role=str(payload.role),
            changed_by=user.id,
        )
    account = db.get(User, user_id)
    assert account is not None  # the membership's foreign key guarantees it
    return _member_out(member, account)


@router.delete("/projects/{project_id}/members/{user_id}", response_model=Message)
def remove_project_member(
    project_id: str, user_id: str, db: DbSession, user: RequireProjectSettings
) -> Message:
    """Take away someone's access to this project.

    Removing the last member is allowed. It does not orphan the project — administrators see
    every project regardless of membership — and refusing would mean a job could never be handed
    over in the obvious order of adding the new team and removing the old one.
    """
    get_project_or_404(db, project_id, user, needs=Capability.SETTINGS_PROJECT)
    member = membership_of(db, user_id, project_id)
    if member is None:
        raise NotFoundError("That user is not a member of this project")

    db.delete(member)
    db.commit()
    log_event(
        "project.member_removed", project_id=project_id, member_id=user_id, removed_by=user.id
    )
    return Message(message="Access removed")
