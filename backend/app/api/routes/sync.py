"""One request that makes a client able to work offline (§32, §26)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import DbSession, RequireProjectRead
from app.schemas.project import (
    BuildingOut,
    FloorOut,
    FloorPlanOut,
    ProjectOut,
    ZoneOut,
)
from app.schemas.sync import ManifestEntry, SyncCollection, SyncResponse
from app.services import sync as sync_service
from app.services.assets import sign

router = APIRouter(tags=["sync"])


def _collection(collection: sync_service.SyncCollection, model: type[Any]) -> Any:
    return SyncCollection(
        changed=[model.model_validate(row) for row in collection.changed],
        manifest=[
            ManifestEntry(id=entry.id, updated_at=entry.updated_at) for entry in collection.manifest
        ],
    )


@router.get("/sync", response_model=SyncResponse)
def sync_reference_data(
    db: DbSession,
    _: RequireProjectRead,
    since: Annotated[
        datetime | None,
        Query(description="The `cursor` from the previous sync. Omit for a full one."),
    ] = None,
) -> SyncResponse:
    """The project tree — projects, buildings, floors, zones, active floor plans — as a delta.

    Exists because the mobile app is offline-first in one direction only: a capture recorded
    without signal is queued and uploaded later, but *starting* one needs three live requests, so
    an operator standing on an unconnected floor cannot pick the floor they are standing on. The
    capture they came to record never begins, and no amount of upload queueing helps because
    nothing is queued yet.

    Each collection comes back as `changed` plus a `manifest` of every row's id and timestamp.
    The manifest is what makes deletion detectable at all — a timestamp-based feed can announce
    a creation or an update and can never announce a row that no longer exists — and it is
    affordable only because reference data is small. Captures are not served this way; see
    `services/sync.py`.

    Floor plan images are not included, only their metadata and a signed URL. A client that wants
    them offline fetches those while it still has a connection; inlining raster bytes would make
    the response the thing it exists to avoid.
    """
    result = sync_service.reference_delta(db, since)

    plans = _collection(result.floor_plans, FloorPlanOut)
    for plan, row in zip(plans.changed, result.floor_plans.changed, strict=True):
        # Signed here rather than in the service, so the URL-minting policy stays in one place
        # and the service stays a plain database read.
        plan.asset = sign(row.storage_key)

    return SyncResponse(
        cursor=result.cursor,
        full=result.full,
        projects=_collection(result.projects, ProjectOut),
        buildings=_collection(result.buildings, BuildingOut),
        floors=_collection(result.floors, FloorOut),
        zones=_collection(result.zones, ZoneOut),
        floor_plans=plans,
    )
