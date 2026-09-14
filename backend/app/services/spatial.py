"""Nearest-capture-location search, indexed by PostGIS where PostGIS exists.

The coordinates involved are floor-plan pixels: a local Cartesian frame with no datum, which is
why the stored geometry carries SRID 0. Treating it as lat/lon would make PostGIS return metres
for a distance that is measured in pixels.

Two implementations, one contract. On PostgreSQL the query is a K-nearest-neighbour scan over the
GiST index using the ``<->`` operator; anywhere else -- SQLite on a laptop, SQLite under the test
suite -- it orders by squared distance over the plain ``x``/``y`` columns. Same rows, same order,
same distances; only the plan differs. Keeping the fallback exact rather than approximate is what
makes it safe for the tests to run on SQLite and still prove the behaviour.
"""

from __future__ import annotations

import math

from sqlalchemy import ColumnElement, Float, cast, func, literal_column, select
from sqlalchemy.orm import Session

from app.models import CaptureLocation

#: Ceiling on rows returned, so a hand-written query string cannot ask for a whole floor.
MAX_RESULTS = 50


def nearest_locations(
    db: Session,
    floor_id: str,
    x: float,
    y: float,
    *,
    limit: int = 1,
    within_px: float | None = None,
) -> list[tuple[CaptureLocation, float]]:
    """Capture locations on ``floor_id`` closest to ``(x, y)``, nearest first.

    Returns ``(location, distance_in_plan_pixels)`` pairs. ``within_px`` filters *after*
    ordering, which is deliberate: the caller usually wants "the nearest point, but only if the
    operator actually clicked near one", and a radius applied in SQL on the fallback path would
    have to be expressed as a squared radius and is easy to get subtly wrong.
    """
    capped = max(1, min(int(limit), MAX_RESULTS))

    if db.bind is not None and db.bind.dialect.name == "postgresql":
        rows = _nearest_postgis(db, floor_id, x, y, capped)
    else:
        rows = _nearest_portable(db, floor_id, x, y, capped)

    if within_px is not None:
        rows = [(location, distance) for location, distance in rows if distance <= within_px]
    return rows


def _nearest_postgis(
    db: Session, floor_id: str, x: float, y: float, limit: int
) -> list[tuple[CaptureLocation, float]]:
    """KNN over the GiST index.

    ``geom`` is a generated column the models deliberately do not map (see the migration), so it
    is named as literal SQL rather than as an attribute. It is qualified with the table name
    because the ORM may add joins to this statement later and an unqualified ``geom`` would then
    be ambiguous rather than merely unusual.
    """
    point = func.ST_SetSRID(func.ST_MakePoint(x, y), 0)
    geom: ColumnElement[object] = literal_column("capture_locations.geom")
    # `<->` is the index-assisted distance operator: ORDER BY on it walks the GiST index instead
    # of computing a distance per row. ST_Distance would give the same number and scan the table.
    distance = geom.op("<->")(point)
    statement = (
        select(CaptureLocation, cast(distance, Float).label("distance"))
        .where(CaptureLocation.floor_id == floor_id)
        .order_by(distance)
        .limit(limit)
    )
    return [(row[0], float(row[1])) for row in db.execute(statement)]


def _nearest_portable(
    db: Session, floor_id: str, x: float, y: float, limit: int
) -> list[tuple[CaptureLocation, float]]:
    """Exact nearest neighbour without a spatial index.

    Squared distance, with the square root taken in Python: ordering by the square is equivalent,
    and SQLite's ``sqrt`` is only present in builds compiled with the math functions, so calling
    it in SQL would work on one machine and raise ``no such function`` on another.
    """
    dx = CaptureLocation.x - x
    dy = CaptureLocation.y - y
    squared = dx * dx + dy * dy
    statement = (
        select(CaptureLocation, squared.label("distance_sq"))
        .where(CaptureLocation.floor_id == floor_id)
        .order_by(squared)
        .limit(limit)
    )
    return [(row[0], math.sqrt(max(0.0, float(row[1])))) for row in db.execute(statement)]
