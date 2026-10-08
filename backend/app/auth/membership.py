"""Which projects a user can reach (§27).

Roles say what a user may do. Until this module existed that was the only question the API
asked, so every authenticated user could read every project in the deployment: a subcontractor's
viewer account could open the main contractor's other jobs, and no role change could prevent it
because "viewer" was never meant to mean "viewer of everything".

Membership is the missing half, and it carries a **role of its own**. A role used to be one
value on the account, so somebody who had to edit one job could edit every job they could see:
the only way to let a subcontractor's lead run their own site was to make them a project manager
everywhere. Now the account role is a ceiling and the membership says what they are on this
project, so the same person can manage one job and only look at the next.

`effective_role` is the single answer to "what is this person here", and it takes the **lower**
of the two every time it is asked rather than trusting what was stored. That matters in one
direction above all: demoting an account has to demote it everywhere at once, without anyone
remembering which five projects the person is on.

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
from app.core.enums import ROLE_RANK, Role
from app.core.errors import NotFoundError, PermissionDeniedError
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


def grant(
    db: Session,
    project_id: str,
    user_id: str,
    *,
    role: Role,
    added_by_id: str | None = None,
) -> bool:
    """Add a member, idempotently. Returns whether a row was created.

    `role` is required and has no default. A default would have to be either the account role,
    which quietly re-creates the deployment-wide role this column exists to end, or `viewer`,
    which silently withholds access somebody meant to give — and whichever was chosen, the call
    sites that did not think about it would be invisible.
    """
    existing = db.scalar(
        select(ProjectMember.id).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    )
    if existing is not None:
        return False
    db.add(
        ProjectMember(project_id=project_id, user_id=user_id, role=role, added_by_id=added_by_id)
    )
    db.flush()
    return True


def membership_of(db: Session, user_id: str, project_id: str) -> ProjectMember | None:
    return db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    )


# ---- what a person is on one project -----------------------------------------
def capped(account_role: Role, project_role: Role) -> Role:
    """The lower of the two, which is the only answer anything here should use.

    One function rather than the same `min` written out in three places, and that is not
    tidiness: it is the line the ceiling actually lives in, so it is the one worth being able to
    break on purpose and watch tests fail. Three do — the role a project reports for the caller,
    the capability list sent with it, and the members list.

    Where it is *not* load-bearing is the authorization check itself, which is worth saying so
    nobody mistakes this for the guard: the route's own signature has already refused anything
    the account cannot do, and the capability sets are nested, so taking the minimum there can
    only re-refuse what was refused. What it protects is the answer to "what can I do here",
    which an interface believes and draws buttons from.
    """
    return min(account_role, project_role, key=lambda role: ROLE_RANK[role])


def effective_role(db: Session, user: User, project_id: str) -> Role | None:
    """The role this user holds **on this project**, or ``None`` if they hold none.

    The lower of the account role and the project role, always. Storing the pair and taking the
    minimum on every read is what makes a demotion take effect everywhere the moment it is
    saved: the alternative is a sweep over every membership, which is the kind of thing that is
    correct on the day it is written and half-applied a year later.

    Exempt accounts (administrators) answer with their account role without a membership row,
    for the reason `sees_every_project` gives.
    """
    if sees_every_project(user):
        return user.role
    member = membership_of(db, user.id, project_id)
    if member is None:
        return None
    return capped(user.role, member.role)


def project_roles_for(db: Session, user: User, project_ids: list[str]) -> dict[str, Role]:
    """Effective roles for a page of projects, in one query rather than one each.

    The project list renders what the caller may do on every row — a "New capture" button that
    turns into a 403 is worse than no button — and asking per row would add a query per project
    to a list that already costs several.
    """
    if sees_every_project(user):
        return dict.fromkeys(project_ids, user.role)
    if not project_ids:
        return {}
    rows = db.execute(
        select(ProjectMember.project_id, ProjectMember.role).where(
            ProjectMember.user_id == user.id, ProjectMember.project_id.in_(project_ids)
        )
    ).all()
    return {project_id: capped(user.role, role) for project_id, role in rows}


def can(db: Session, user: User, project_id: str, capability: Capability) -> bool:
    role = effective_role(db, user, project_id)
    return role is not None and has_capability(role, capability)


def assert_can(
    db: Session,
    user: User,
    project_id: str,
    capability: Capability,
    *,
    what: str = "Project",
) -> Role:
    """Raise unless this user may do this **here**. Returns the role they are doing it with.

    Two different refusals, because they answer two different people. Someone who is not a member
    gets the 404 `assert_member` gives, for the reason written there — the existence of a project
    is itself worth not confirming. Someone who *is* a member and lacks the capability gets a 403
    naming the role they hold on this project, because they can see the job, they are looking at
    a button that did not work, and "not found" would be a lie they cannot act on.
    """
    role = effective_role(db, user, project_id)
    if role is None:
        raise NotFoundError(f"{what} not found")
    if not has_capability(role, capability):
        raise PermissionDeniedError(
            f"Your role on this project ({role}) cannot perform this action",
            required_capability=str(capability),
            project_role=str(role),
        )
    return role
