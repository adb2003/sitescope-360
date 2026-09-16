"""Reference-data delta sync for offline-first clients (§32).

The mobile app is offline-first in one direction only. A capture recorded without signal is held
in SQLite and uploaded later, which is the hard half — but *setting up* that capture still needs
three live requests (`/projects`, `/projects/{id}/floors`, `/floors/{id}/zones`). An operator who
arrives on a half-built floor with no signal cannot choose the floor they are standing on, so the
capture they came to record never starts. Nothing about the upload queue helps, because there is
nothing queued yet.

This endpoint answers the whole question in one request, and answers it *incrementally* so that
opening the app on site is cheap rather than a full re-download of the project tree.

**How deletions are handled, which is the part that decides the design.** A delta built on
``updated_at`` can report creations and updates and can never report a deletion: the row is gone,
so there is nothing left to have a timestamp. A client that only ever applies changes therefore
accumulates floors that no longer exist, and offers the operator a floor whose captures will be
rejected. The usual fix is a tombstone table written by every delete path — correct, and a
migration plus a new obligation on code that deletes things.

Instead, every response carries a **manifest**: the id and ``updated_at`` of every row the client
should be holding, whether or not it changed. Anything the client has that is absent from the
manifest was deleted; anything whose manifest timestamp is newer than the client's copy was
missed. So the protocol converges exactly, with no tombstones and no trust in the client's clock.

That is only affordable because this data is *small* — a project has tens of floors, not
thousands — and it is the reason :data:`MANIFEST_LIMIT` exists rather than being left implicit.
Captures and frames are deliberately **not** synced this way: they are output rather than
reference data, they run to thousands of rows, and `/captures?since=` already pages through them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.models import Building, Floor, FloorPlan, Project, Zone

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import InstrumentedAttribute

#: Above this many reference rows the manifest stops being the cheap option and the honest answer
#: is a tombstone table, not a bigger payload. Raising it silently would turn a 5 KB response into
#: a multi-megabyte one on the connection least able to carry it — so it fails loudly instead.
MANIFEST_LIMIT = 20_000

#: How far behind the present the cursor is set, and why it has to be behind at all.
#:
#: A row's ``updated_at`` can be *earlier* than a cursor taken after the row was written, for two
#: unrelated reasons:
#:
#: * **Granularity.** SQLite's ``CURRENT_TIMESTAMP`` has one-second resolution, so a row written
#:   at 12:00:00.9 is stored as 12:00:00.0 — before a cursor of 12:00:00.7 taken moments earlier.
#: * **Transaction timing.** PostgreSQL's ``now()`` is the *transaction start*, so a write that
#:   began before this sync and committed after it is stamped earlier than the cursor and was
#:   invisible to the query.
#:
#: Either way, asking next time for ``updated_at >= cursor`` would skip that row permanently. The
#: first version of this endpoint did exactly that, and the test that caught it is the one
#: checking a second sync carries a row created just after the first. Five seconds covers a
#: second of granularity and the short write transactions this application actually runs.
#:
#: It is not the correctness guarantee, though — the manifest is. A write slower than this lag
#: still leaves a manifest entry newer than the client's copy, which is how the client finds out
#: and resynchronises. The lag exists so that recovery is rare, not so that it is unnecessary.
CURSOR_LAG = timedelta(seconds=5)

_ROW = TypeVar("_ROW")


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One row the client should be holding, and when it last changed."""

    id: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SyncCollection:
    """One collection's delta: what changed, and what the full set now is."""

    changed: list[Any]
    manifest: list[ManifestEntry]


@dataclass(frozen=True, slots=True)
class SyncResult:
    cursor: datetime
    full: bool
    projects: SyncCollection
    buildings: SyncCollection
    floors: SyncCollection
    zones: SyncCollection
    floor_plans: SyncCollection


def _manifest(
    db: Session,
    id_column: InstrumentedAttribute[str],
    updated_column: InstrumentedAttribute[datetime],
    query: Select[Any] | None = None,
) -> list[ManifestEntry]:
    base = query if query is not None else select(id_column, updated_column)
    rows = db.execute(base).all()
    return [ManifestEntry(id=str(row[0]), updated_at=row[1]) for row in rows]


def _comparable(since: datetime) -> datetime:
    """Widen ``since`` to a value that compares correctly against a stored timestamp.

    SQLite has no date type: it compares DATETIME columns as **text**. ``updated_at`` is written
    by ``func.now()`` → ``CURRENT_TIMESTAMP``, which has no fractional part, while a bound Python
    datetime is rendered with microseconds — so ``'…12:00:01' >= '…12:00:01.000000'`` is false as
    a string comparison, and every row written in the boundary second is skipped. Flooring alone
    does not fix it, because the bound value still renders its zeros; the value has to fall in an
    earlier second than anything it should match.

    One second back, floored. It only ever widens the window, which costs a few re-sent rows an
    upsert absorbs — where narrowing it loses rows permanently. Harmless on PostgreSQL, which
    compares real timestamps and simply sees a slightly earlier bound.
    """
    return since.replace(microsecond=0) - timedelta(seconds=1)


def _collection(
    db: Session,
    *,
    model: type[_ROW],
    since: datetime | None,
    scope: Any = None,
) -> SyncCollection:
    """One collection, as ``(rows that changed, every row's id and timestamp)``.

    Two queries rather than one. The manifest could be derived from the changed rows plus a
    separate id list, but the changed set is normally tiny and the manifest is always complete —
    keeping them apart is what lets the response be small on the common sync and still say
    everything the client needs to reconcile.
    """
    id_column = model.id  # type: ignore[attr-defined]
    updated_column = model.updated_at  # type: ignore[attr-defined]

    manifest_query = select(id_column, updated_column)
    changed_query = select(model)
    if scope is not None:
        manifest_query = manifest_query.where(scope)
        changed_query = changed_query.where(scope)
    if since is not None:
        # Inclusive, and against a widened bound. Between this, `CURSOR_LAG` and `_comparable`
        # a sync re-sends a few seconds of rows every time, which an upsert absorbs for nothing —
        # where any of the three being too tight drops a row permanently.
        changed_query = changed_query.where(updated_column >= _comparable(since))

    manifest = _manifest(db, id_column, updated_column, manifest_query)
    changed = list(db.scalars(changed_query))
    return SyncCollection(changed=changed, manifest=manifest)


def reference_delta(db: Session, since: datetime | None = None) -> SyncResult:
    """Everything a client needs to run offline, changed since ``since``.

    The cursor is deliberately :data:`CURSOR_LAG` behind the present, so the next call's window
    overlaps this one. A row can carry a timestamp earlier than the moment it became visible —
    coarse clock resolution, or a transaction stamped at its start and committed later — and a
    cursor taken at the present would step straight over it.
    """
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        if since > datetime.now(UTC):
            # A client clock running fast would otherwise ask for changes from the future and be
            # told there are none, indefinitely.
            raise ValidationError("`since` is in the future; omit it to resynchronise")

    cursor = datetime.now(UTC) - CURSOR_LAG

    projects = _collection(db, model=Project, since=since)
    buildings = _collection(db, model=Building, since=since)
    floors = _collection(db, model=Floor, since=since)
    zones = _collection(db, model=Zone, since=since)
    # Superseded plans are excluded from both halves, so a plan being replaced leaves the
    # manifest and the client deletes its copy -- the same path a deletion takes. An operator
    # must not be looking at last month's plan because it is still on the device.
    plans = _collection(db, model=FloorPlan, since=since, scope=FloorPlan.is_active.is_(True))

    total = sum(
        len(collection.manifest) for collection in (projects, buildings, floors, zones, plans)
    )
    if total > MANIFEST_LIMIT:
        raise ValidationError(
            f"This deployment has {total} reference rows, over the {MANIFEST_LIMIT} this "
            "endpoint can reconcile in one response. Deletion tracking needs tombstones at "
            "this size; see app/services/sync.py."
        )

    return SyncResult(
        cursor=cursor,
        full=since is None,
        projects=projects,
        buildings=buildings,
        floors=floors,
        zones=zones,
        floor_plans=plans,
    )
