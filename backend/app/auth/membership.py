"""Which projects a user can reach (§27).

Roles say what a user may do. Until this module existed that was the only question the API
asked, so every authenticated user could read every project in the deployment: a subcontractor's
viewer account could open the main contractor's other jobs, and no role change could prevent it
because "viewer" was never meant to mean "viewer of everything".

Membership is the missing half. It is not a second role — a user's global role still decides
their capabilities, and this decides which projects those capabilities reach. Keeping the two
separate is what stops this becoming a redesign of the §27 matrix.

**Enforced through the resource resolvers**, not route by route. `app.api.deps` already loads the
project behind every path — a frame resolves to a session, a session to a project — and the
membership check lives there, so adding a route that reads a frame cannot forget it. The `user`
argument on those resolvers is required rather than defaulted for the same reason: a missed call
site is a type error, not a silent authorization hole.

Two things this module deliberately does *not* do:

* **Filter list endpoints.** Those never resolve a single resource, so there is nothing for a
  resolver to check. They ask for `visible_project_ids` explicitly, which is visible in the query
  and reviewable.
* **Hide a project's existence from a member of nothing.** A non-member gets 404 rather than 403,
  because "you are not a member of project X" tells a stranger that project X exists. The
  distinction only matters to a caller probing for names, and that is the caller it matters for.
"""

from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.auth.rbac import Capability, has_capability
from app.core.errors import NotFoundError
from app.models import Building, Floor, ProjectMember, User


def sees_every_project(user: User) -> bool:
    """Is this user exempt from membership entirely?

    Administrators are. They hold `settings:system` and `user:manage`, so a deployment where an
    admin could be shut out of a project would have an administrator unable to administer it —
    and the person who would have to fix it is the one who is locked out. Derived from the
    capability rather than from `role == admin` so it follows the §27 matrix if that changes.
    """
    return has_capability(user.role, Capability.SETTINGS_SYSTEM)


def visible_project_ids(db: Session, user: User) -> list[str] | None:
    """The projects this user may see, or ``None`` meaning "no restriction".

    ``None`` rather than a list of every id: the exempt case should not turn into an ``IN`` clause
    listing a thousand projects, and a caller that forgets to handle it gets an obvious failure
    rather than a quietly empty result.
    """
    if sees_every_project(user):
        return None
    return list(
        db.scalars(select(ProjectMember.project_id).where(ProjectMember.user_id == user.id))
    )


def is_member(db: Session, user: User, project_id: str) -> bool:
    if sees_every_project(user):
        return True
    return (
        db.scalar(
            select(ProjectMember.id)
            .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id)
            .limit(1)
        )
        is not None
    )


def assert_member(db: Session, user: User, project_id: str, *, what: str = "Project") -> None:
    """Raise if this user cannot reach this project.

    ``NotFoundError`` and not ``PermissionDeniedError``: a 403 on a project a stranger has never
    been told about confirms that it exists, and its id is often a guessable code. A member who
    has genuinely lost access sees the same 404, which is a worse message for them and the right
    trade — they can ask, and a prober cannot enumerate.
    """
    if not is_member(db, user, project_id):
        raise NotFoundError(f"{what} not found")


def restrict_to_member_projects(query: Select, column, db: Session, user: User) -> Select:
    """Narrow a list query to the caller's projects, or leave it alone for an exempt user.

    ``column`` is whatever holds the project id on the thing being listed. An empty membership
    yields ``IN ()``, which returns nothing — correct, and the reason a list endpoint must not
    treat "no rows" as "no data exists".
    """
    allowed = visible_project_ids(db, user)
    if allowed is None:
        return query
    return query.where(column.in_(allowed))


def member_floor_ids(db: Session, user: User) -> list[str] | None:
    """Floor ids inside the caller's projects, or ``None`` for an exempt user.

    For the collections that hang off a floor rather than a project — zones, plans, locations —
    where the alternative is a three-table join repeated at every call site.
    """
    allowed = visible_project_ids(db, user)
    if allowed is None:
        return None
    return list(
        db.scalars(
            select(Floor.id)
            .join(Building, Building.id == Floor.building_id)
            .where(Building.project_id.in_(allowed))
        )
    )


def grant(db: Session, project_id: str, user_id: str, *, added_by_id: str | None = None) -> bool:
    """Add a member, idempotently. Returns whether a row was created."""
    existing = db.scalar(
        select(ProjectMember.id).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    )
    if existing is not None:
        return False
    db.add(ProjectMember(project_id=project_id, user_id=user_id, added_by_id=added_by_id))
    db.flush()
    return True
