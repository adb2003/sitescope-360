"""Role-based authorization (§27).

Roles are strictly ordered, so a single ``require_role(minimum)`` covers the whole matrix instead
of enumerating role sets at every route. Where the matrix is *not* a simple ordering — a Site
Engineer may create captures but not projects — the capability is named explicitly below and
checked by name, which keeps the intent readable at the call site.
"""

from __future__ import annotations

from enum import StrEnum

from app.core.enums import ROLE_RANK, Role


class Capability(StrEnum):
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    PROJECT_DELETE = "project:delete"
    CAPTURE_READ = "capture:read"
    CAPTURE_CREATE = "capture:create"
    CAPTURE_DELETE = "capture:delete"
    UPLOAD = "upload"
    ANNOTATE = "annotate"
    ANNOTATION_RESOLVE = "annotation:resolve"
    FLOORPLAN_EDIT = "floorplan:edit"
    LOCATION_EDIT = "location:edit"
    REPORT_GENERATE = "report:generate"
    SETTINGS_PROJECT = "settings:project"
    SETTINGS_SYSTEM = "settings:system"
    USER_MANAGE = "user:manage"
    JOB_ADMIN = "job:admin"


#: The §27 permission table, expressed once.
_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.VIEWER: frozenset(
        {
            Capability.PROJECT_READ,
            Capability.CAPTURE_READ,
        }
    ),
    Role.SITE_ENGINEER: frozenset(
        {
            Capability.PROJECT_READ,
            Capability.CAPTURE_READ,
            Capability.CAPTURE_CREATE,
            Capability.UPLOAD,
            Capability.ANNOTATE,
            Capability.LOCATION_EDIT,
        }
    ),
    Role.PROJECT_MANAGER: frozenset(
        {
            Capability.PROJECT_READ,
            Capability.PROJECT_WRITE,
            Capability.CAPTURE_READ,
            Capability.CAPTURE_CREATE,
            Capability.CAPTURE_DELETE,
            Capability.UPLOAD,
            Capability.ANNOTATE,
            Capability.ANNOTATION_RESOLVE,
            Capability.FLOORPLAN_EDIT,
            Capability.LOCATION_EDIT,
            Capability.REPORT_GENERATE,
            Capability.SETTINGS_PROJECT,
        }
    ),
    Role.ADMIN: frozenset(Capability),
}


def capabilities_for(role: Role) -> frozenset[Capability]:
    return _CAPABILITIES.get(role, frozenset())


def has_capability(role: Role, capability: Capability) -> bool:
    return capability in capabilities_for(role)


def role_at_least(role: Role, minimum: Role) -> bool:
    return ROLE_RANK[role] >= ROLE_RANK[minimum]
