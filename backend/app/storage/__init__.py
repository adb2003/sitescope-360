"""Storage provider registry."""

from __future__ import annotations

from functools import lru_cache

from app.core.config import settings
from app.storage.base import (
    StorageProvider,
    StoredObject,
    build_key,
    extension_of,
    validate_key,
)
from app.storage.local import LocalStorage


@lru_cache
def get_storage() -> StorageProvider:
    """Resolve the configured provider. Cached — providers are stateless and thread-safe."""
    if settings.storage_backend == "s3":
        from app.storage.s3 import S3Storage

        return S3Storage()
    return LocalStorage()


# ---- key helpers -------------------------------------------------------------
# One place that knows the key layout, so nothing else has to string-concatenate paths.


def session_key(project_id: str, floor_id: str, session_id: str, kind: str, filename: str) -> str:
    return build_key(
        "projects", project_id, "floors", floor_id, "sessions", session_id, kind, filename
    )


def session_prefix(project_id: str, floor_id: str, session_id: str, kind: str) -> str:
    """The directory every ``kind`` object for one session lives under.

    Built through :func:`build_key` like any other key, so the same validation applies. It exists
    because asking for the prefix by passing an empty filename would be rejected -- correctly,
    since an empty segment is exactly the kind of key that resolves to a parent directory.
    """
    return build_key("projects", project_id, "floors", floor_id, "sessions", session_id, kind)


def floorplan_key(project_id: str, floor_id: str, filename: str) -> str:
    return build_key("projects", project_id, "floors", floor_id, "floorplans", filename)


def report_key(project_id: str, filename: str) -> str:
    return build_key("projects", project_id, "reports", filename)


__all__ = [
    "LocalStorage",
    "StorageProvider",
    "StoredObject",
    "build_key",
    "extension_of",
    "floorplan_key",
    "get_storage",
    "report_key",
    "session_key",
    "validate_key",
]
