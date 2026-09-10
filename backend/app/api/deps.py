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

from app.auth.rbac import Capability, has_capability, role_at_least
from app.auth.security import decode_access_token
from app.core.enums import Role
from app.core.errors import AuthenticationError, NotFoundError, PermissionDeniedError
from app.database.session import get_db
from app.models import Building, CaptureFrame, CaptureSession, Floor, Project, User
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
def get_project_or_404(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")
    return project


def get_building_or_404(db: Session, building_id: str) -> Building:
    building = db.get(Building, building_id)
    if building is None:
        raise NotFoundError(f"Building {building_id} not found")
    return building


def get_floor_or_404(db: Session, floor_id: str) -> Floor:
    floor = db.get(Floor, floor_id)
    if floor is None:
        raise NotFoundError(f"Floor {floor_id} not found")
    return floor


def get_session_or_404(db: Session, session_id: str) -> CaptureSession:
    session = db.get(CaptureSession, session_id)
    if session is None:
        raise NotFoundError(f"Capture session {session_id} not found")
    return session


def get_frame_or_404(db: Session, frame_id: str) -> CaptureFrame:
    frame = db.get(CaptureFrame, frame_id)
    if frame is None:
        raise NotFoundError(f"Frame {frame_id} not found")
    return frame


def project_id_for_floor(db: Session, floor_id: str) -> str:
    row = db.execute(
        select(Building.project_id)
        .join(Floor, Floor.building_id == Building.id)
        .where(Floor.id == floor_id)
    ).first()
    if row is None:
        raise NotFoundError(f"Floor {floor_id} not found")
    return str(row[0])


def db_iterator() -> Iterator[Session]:  # pragma: no cover - re-export for clarity
    yield from get_db()
