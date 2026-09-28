"""FastAPI dependencies: authentication, authorization, pagination, resource resolution.

Authorization is declared *at the route* as a dependency rather than checked inside services, so
a new endpoint cannot accidentally ship without a guard — the signature is the guard.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.membership import assert_member
from app.auth.rbac import Capability, has_capability, role_at_least
from app.auth.security import decode_access_token
from app.core.enums import Role
from app.core.errors import AuthenticationError, NotFoundError, PermissionDeniedError
from app.database.session import get_db
from app.models import (
    Building,
    CaptureFile,
    CaptureFrame,
    CaptureLocation,
    CaptureSession,
    Floor,
    FloorPlan,
    Project,
    Report,
    User,
    Zone,
)
from app.schemas.common import PageParams

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authentication required")

    claims = decode_access_token(credentials.credentials)
    user = db.get(User, claims.subject)
    if user is None:
        raise AuthenticationError("User no longer exists")
    if not user.is_active:
        raise AuthenticationError("This account has been deactivated")
    # A role change must take effect immediately, not when the token happens to expire.
    if user.role != claims.role:
        raise AuthenticationError("Your permissions changed — please sign in again")

    request.state.user_id = user.id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(minimum: Role) -> Callable[[User], User]:
    def dependency(user: CurrentUser) -> User:
        if not role_at_least(user.role, minimum):
            raise PermissionDeniedError(
                f"This action requires the {minimum} role or higher",
                your_role=str(user.role),
            )
        return user

    return dependency


def require_capability(capability: Capability) -> Callable[[User], User]:
    def dependency(user: CurrentUser) -> User:
        if not has_capability(user.role, capability):
            raise PermissionDeniedError(
                f"Your role ({user.role}) cannot perform this action",
                required_capability=str(capability),
            )
        return user

    return dependency


# Pre-built guards for the capabilities used across the API.
RequireProjectRead = Annotated[User, Depends(require_capability(Capability.PROJECT_READ))]
RequireProjectWrite = Annotated[User, Depends(require_capability(Capability.PROJECT_WRITE))]
RequireProjectDelete = Annotated[User, Depends(require_capability(Capability.PROJECT_DELETE))]
RequireCaptureRead = Annotated[User, Depends(require_capability(Capability.CAPTURE_READ))]
RequireCaptureCreate = Annotated[User, Depends(require_capability(Capability.CAPTURE_CREATE))]
RequireCaptureDelete = Annotated[User, Depends(require_capability(Capability.CAPTURE_DELETE))]
RequireUpload = Annotated[User, Depends(require_capability(Capability.UPLOAD))]
RequireAnnotate = Annotated[User, Depends(require_capability(Capability.ANNOTATE))]
RequireFloorPlanEdit = Annotated[User, Depends(require_capability(Capability.FLOORPLAN_EDIT))]
RequireLocationEdit = Annotated[User, Depends(require_capability(Capability.LOCATION_EDIT))]
RequireReportGenerate = Annotated[User, Depends(require_capability(Capability.REPORT_GENERATE))]
RequireProjectSettings = Annotated[User, Depends(require_capability(Capability.SETTINGS_PROJECT))]
RequireSystemSettings = Annotated[User, Depends(require_capability(Capability.SETTINGS_SYSTEM))]
RequireUserManage = Annotated[User, Depends(require_capability(Capability.USER_MANAGE))]
RequireJobAdmin = Annotated[User, Depends(require_capability(Capability.JOB_ADMIN))]


def pagination(
    limit: Annotated[int, Query(ge=1, le=200, description="Maximum items to return")] = 50,
    offset: Annotated[int, Query(ge=0, description="Items to skip")] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


Pagination = Annotated[PageParams, Depends(pagination)]


# ---- resource resolution -----------------------------------------------------
# Resolving the parent chain in one place means a 404 is consistent and no route has to
# hand-roll a join to find the project a frame belongs to.
#
# It is also where **project membership** is enforced (§27). Roles say what a user may do;
# membership says which projects they may do it to, and every path into project data passes
# through one of these resolvers on its way to a row. Checking here rather than in each route
# means a new endpoint that loads a frame cannot ship without the guard — the same reasoning
# that puts capability checks in the signature.
#
# `user` is a **required** argument on every resolver, deliberately. Given a default it would be
# possible to add a call site that quietly skips the check and passes review; required, a missed
# one is a type error before it is a security hole.
def get_project_or_404(db: Session, project_id: str, user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")
    assert_member(db, user, project.id, what=f"Project {project_id}")
    return project


def get_building_or_404(db: Session, building_id: str, user: User) -> Building:
    building = db.get(Building, building_id)
    if building is None:
        raise NotFoundError(f"Building {building_id} not found")
    assert_member(db, user, building.project_id, what=f"Building {building_id}")
    return building


def get_floor_or_404(db: Session, floor_id: str, user: User) -> Floor:
    floor = db.get(Floor, floor_id)
    if floor is None:
        raise NotFoundError(f"Floor {floor_id} not found")
    assert_member(db, user, _project_of_floor(db, floor), what=f"Floor {floor_id}")
    return floor


def get_session_or_404(db: Session, session_id: str, user: User) -> CaptureSession:
    session = db.get(CaptureSession, session_id)
    if session is None:
        raise NotFoundError(f"Capture session {session_id} not found")
    assert_member(db, user, session.project_id, what=f"Capture session {session_id}")
    return session


def get_frame_or_404(db: Session, frame_id: str, user: User) -> CaptureFrame:
    frame = db.get(CaptureFrame, frame_id)
    if frame is None:
        raise NotFoundError(f"Frame {frame_id} not found")
    # One extra lookup rather than a join: the session is almost always already in the identity
    # map by the time a frame route needs it, and the join would be re-written at every caller.
    session = db.get(CaptureSession, frame.session_id)
    if session is None:
        raise NotFoundError(f"Frame {frame_id} not found")
    assert_member(db, user, session.project_id, what=f"Frame {frame_id}")
    return frame


def project_id_for_floor(db: Session, floor_id: str, user: User) -> str:
    row = db.execute(
        select(Building.project_id)
        .join(Floor, Floor.building_id == Building.id)
        .where(Floor.id == floor_id)
    ).first()
    if row is None:
        raise NotFoundError(f"Floor {floor_id} not found")
    project_id = str(row[0])
    assert_member(db, user, project_id, what=f"Floor {floor_id}")
    return project_id


def get_zone_or_404(db: Session, zone_id: str, user: User) -> Zone:
    zone = db.get(Zone, zone_id)
    if zone is None:
        raise NotFoundError(f"Zone {zone_id} not found")
    assert_member(db, user, project_id_for_floor(db, zone.floor_id, user), what=f"Zone {zone_id}")
    return zone


def get_plan_or_404(db: Session, plan_id: str, user: User) -> FloorPlan:
    plan = db.get(FloorPlan, plan_id)
    if plan is None:
        raise NotFoundError(f"Floor plan {plan_id} not found")
    project_id_for_floor(db, plan.floor_id, user)
    return plan


def get_location_or_404(db: Session, location_id: str, user: User) -> CaptureLocation:
    location = db.get(CaptureLocation, location_id)
    if location is None:
        raise NotFoundError(f"Location {location_id} not found")
    project_id_for_floor(db, location.floor_id, user)
    return location


def get_report_or_404(db: Session, report_id: str, user: User) -> Report:
    record = db.get(Report, report_id)
    if record is None:
        raise NotFoundError(f"Report {report_id} not found")
    assert_member(db, user, record.project_id, what=f"Report {report_id}")
    return record


def get_capture_file_or_404(db: Session, file_id: str, user: User) -> CaptureFile:
    record = db.get(CaptureFile, file_id)
    if record is None:
        raise NotFoundError(f"File {file_id} not found")
    get_session_or_404(db, record.session_id, user)
    return record


def _project_of_floor(db: Session, floor: Floor) -> str:
    building = db.get(Building, floor.building_id)
    if building is None:
        raise NotFoundError(f"Floor {floor.id} not found")
    return building.project_id


def db_iterator() -> Iterator[Session]:  # pragma: no cover - re-export for clarity
    yield from get_db()
