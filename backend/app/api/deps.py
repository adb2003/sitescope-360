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

from app.auth.membership import assert_can
from app.auth.rbac import Capability, has_capability, role_at_least
from app.auth.security import decode_access_token
from app.core.enums import Role
from app.core.errors import (
    AuthenticationError,
    NotFoundError,
    PasswordChangeRequiredError,
    PermissionDeniedError,
)
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


def _authenticated_user(
    request: Request,
    db: Session,
    credentials: HTTPAuthorizationCredentials | None,
) -> User:
    """Resolve the bearer token to a live account, without asking what it may do."""
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


def get_current_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    """The ordinary dependency: an account that may actually use the platform.

    An account whose password was reset by an administrator is held here. The alternative —
    refusing at sign-in — would leave the holder with no way to use the credential they were
    given, and flagging only the new token would let a session opened beforehand carry on
    indefinitely. Checking the row on every request means the requirement cannot be outlived.
    """
    user = _authenticated_user(request, db, credentials)
    if user.must_change_password:
        raise PasswordChangeRequiredError(
            "Your password was reset by an administrator. Set a new one before continuing."
        )
    return user


def get_user_setting_password(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    """The same account, for the few endpoints a held account must still reach.

    Deliberately a *second dependency* rather than an exemption inside the first. An allow-list
    consulted by route name would put the decision next to the check, where a later route added
    to the list by accident is invisible; this way an endpoint is reachable by a held account
    only because its signature says so, and every other endpoint is protected by not saying so.
    """
    return _authenticated_user(request, db, credentials)


CurrentUser = Annotated[User, Depends(get_current_user)]
#: For `/auth/me`, `/auth/change-password` and nothing else: see `get_user_setting_password`.
UserSettingPassword = Annotated[User, Depends(get_user_setting_password)]


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
# It is also where **project authorization** is enforced (§27), and since a role became a
# per-project thing it is the only place that can be. The capability in a route's signature is
# checked against the account, which is now a ceiling; the role that actually decides is the one
# held on whichever project this request turns out to touch, and the resolver is where that
# project becomes known.
#
# `user` is a **required** argument on every resolver, deliberately. Given a default it would be
# possible to add a call site that quietly skips the check and passes review; required, a missed
# one is a type error before it is a security hole.
#
# `needs` is **not** required, and the asymmetry is deliberate rather than an oversight. Its
# default is the resolver's own read capability, which every member holds by definition — the
# lowest role is `viewer`, and `viewer` has both reads — so a resolver called without it performs
# exactly the membership check these functions have always performed. A route that *writes* must
# name what it is doing, and `test_project_roles.py` reads the route table to check that each one
# does: a write route whose signature demands `floorplan:edit` while its resolver asks for
# `project:read` would be judged on the account role alone, which is the hole this closes.
def get_project_or_404(
    db: Session, project_id: str, user: User, *, needs: Capability = Capability.PROJECT_READ
) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")
    assert_can(db, user, project.id, needs, what=f"Project {project_id}")
    return project


def get_building_or_404(
    db: Session, building_id: str, user: User, *, needs: Capability = Capability.PROJECT_READ
) -> Building:
    building = db.get(Building, building_id)
    if building is None:
        raise NotFoundError(f"Building {building_id} not found")
    assert_can(db, user, building.project_id, needs, what=f"Building {building_id}")
    return building


def get_floor_or_404(
    db: Session, floor_id: str, user: User, *, needs: Capability = Capability.PROJECT_READ
) -> Floor:
    floor = db.get(Floor, floor_id)
    if floor is None:
        raise NotFoundError(f"Floor {floor_id} not found")
    assert_can(db, user, _project_of_floor(db, floor), needs, what=f"Floor {floor_id}")
    return floor


def get_session_or_404(
    db: Session, session_id: str, user: User, *, needs: Capability = Capability.CAPTURE_READ
) -> CaptureSession:
    session = db.get(CaptureSession, session_id)
    if session is None:
        raise NotFoundError(f"Capture session {session_id} not found")
    assert_can(db, user, session.project_id, needs, what=f"Capture session {session_id}")
    return session


def get_frame_or_404(
    db: Session, frame_id: str, user: User, *, needs: Capability = Capability.CAPTURE_READ
) -> CaptureFrame:
    frame = db.get(CaptureFrame, frame_id)
    if frame is None:
        raise NotFoundError(f"Frame {frame_id} not found")
    # One extra lookup rather than a join: the session is almost always already in the identity
    # map by the time a frame route needs it, and the join would be re-written at every caller.
    session = db.get(CaptureSession, frame.session_id)
    if session is None:
        raise NotFoundError(f"Frame {frame_id} not found")
    assert_can(db, user, session.project_id, needs, what=f"Frame {frame_id}")
    return frame


def project_id_for_floor(
    db: Session, floor_id: str, user: User, *, needs: Capability = Capability.PROJECT_READ
) -> str:
    row = db.execute(
        select(Building.project_id)
        .join(Floor, Floor.building_id == Building.id)
        .where(Floor.id == floor_id)
    ).first()
    if row is None:
        raise NotFoundError(f"Floor {floor_id} not found")
    project_id = str(row[0])
    assert_can(db, user, project_id, needs, what=f"Floor {floor_id}")
    return project_id


def get_zone_or_404(
    db: Session, zone_id: str, user: User, *, needs: Capability = Capability.PROJECT_READ
) -> Zone:
    zone = db.get(Zone, zone_id)
    if zone is None:
        raise NotFoundError(f"Zone {zone_id} not found")
    project_id_for_floor(db, zone.floor_id, user, needs=needs)
    return zone


def get_plan_or_404(
    db: Session, plan_id: str, user: User, *, needs: Capability = Capability.PROJECT_READ
) -> FloorPlan:
    plan = db.get(FloorPlan, plan_id)
    if plan is None:
        raise NotFoundError(f"Floor plan {plan_id} not found")
    project_id_for_floor(db, plan.floor_id, user, needs=needs)
    return plan


def get_location_or_404(
    db: Session, location_id: str, user: User, *, needs: Capability = Capability.PROJECT_READ
) -> CaptureLocation:
    location = db.get(CaptureLocation, location_id)
    if location is None:
        raise NotFoundError(f"Location {location_id} not found")
    project_id_for_floor(db, location.floor_id, user, needs=needs)
    return location


def get_report_or_404(
    db: Session, report_id: str, user: User, *, needs: Capability = Capability.PROJECT_READ
) -> Report:
    record = db.get(Report, report_id)
    if record is None:
        raise NotFoundError(f"Report {report_id} not found")
    assert_can(db, user, record.project_id, needs, what=f"Report {report_id}")
    return record


def get_capture_file_or_404(
    db: Session, file_id: str, user: User, *, needs: Capability = Capability.CAPTURE_READ
) -> CaptureFile:
    record = db.get(CaptureFile, file_id)
    if record is None:
        raise NotFoundError(f"File {file_id} not found")
    get_session_or_404(db, record.session_id, user, needs=needs)
    return record


def _project_of_floor(db: Session, floor: Floor) -> str:
    building = db.get(Building, floor.building_id)
    if building is None:
        raise NotFoundError(f"Floor {floor.id} not found")
    return building.project_id


def db_iterator() -> Iterator[Session]:  # pragma: no cover - re-export for clarity
    yield from get_db()
