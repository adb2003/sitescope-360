"""Response shape for the offline reference-data sync (§32, §26)."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.schemas.project import (
    BuildingOut,
    FloorOut,
    FloorPlanOut,
    ProjectOut,
    ZoneOut,
)

ItemT = TypeVar("ItemT")


class ManifestEntry(BaseModel):
    """One row the client should be holding, and when it last changed.

    Two fields and both load-bearing. The **id** is how a deletion is detected: a row the client
    has that is not in the manifest no longer exists, which a change feed built on timestamps can
    never say on its own. The **timestamp** is how a missed update is detected: newer here than
    on the client means the change feed did not carry it, and the client should resynchronise in
    full rather than keep serving a stale record.
    """

    id: str
    updated_at: datetime


class SyncCollection(BaseModel, Generic[ItemT]):
    """One collection's delta.

    ``changed`` holds full records — apply them as upserts, since the same row may legitimately
    arrive twice across two syncs. ``manifest`` always describes the *complete* set, so anything
    held locally and missing from it should be deleted.
    """

    changed: list[ItemT] = Field(default_factory=list)
    manifest: list[ManifestEntry] = Field(default_factory=list)


class SyncResponse(BaseModel):
    """Everything a client needs to work offline, as of ``cursor``.

    ``cursor`` is taken before the queries run and is what to send as ``since`` next time. It is
    deliberately a little early: a transaction that stamped ``updated_at`` before this sync began
    and committed after it ended is invisible to this response, and a cursor taken at the end
    would make it invisible for ever. The overlap re-sends a few rows instead, which an upsert
    absorbs.

    ``full`` says whether this response is the entire reference set. It is true when ``since``
    was omitted, and is what tells a client it may replace its cache outright rather than
    reconcile against it.
    """

    cursor: datetime
    full: bool
    projects: SyncCollection[ProjectOut]
    buildings: SyncCollection[BuildingOut]
    floors: SyncCollection[FloorOut]
    zones: SyncCollection[ZoneOut]
    floor_plans: SyncCollection[FloorPlanOut]
