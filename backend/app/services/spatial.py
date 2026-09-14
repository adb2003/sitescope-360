"""Nearest-capture-location search, indexed by PostGIS where PostGIS exists.

The coordinates involved are floor-plan pixels: a local Cartesian frame with no datum, which is
why the stored geometry carries SRID 0. Treating it as lat/lon would make PostGIS return metres
for a distance that is measured in pixels.

Two implementations, one contract. On PostgreSQL the query is a K-nearest-neighbour scan over the
GiST index using the ``<->`` operator, with any radius pushed down as ``ST_DWithin`` so the index
can prune; anywhere else -- SQLite on a laptop, SQLite under the test suite -- it orders by
squared distance over the plain ``x``/``y`` columns and compares against a squared radius. Same
rows, same order, same distances; only the plan differs. Keeping the fallback exact rather than
approximate is what makes it safe for the tests to run on SQLite and still prove the behaviour.
"""

from __future__ import annotations

import math

from sqlalchemy import ColumnElement, Float, cast, func, literal_column, select
from sqlalchemy.orm import Session

from app.models import CaptureFrame, CaptureLocation

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

    Returns ``(location, distance_in_plan_pixels)`` pairs.

    ``within_px`` is part of the query rather than a filter applied to its results, for two
    reasons. It has to run before ``LIMIT`` or "everything within this radius" silently becomes
    "whichever of the nearest N happened to fall inside it" — a different answer whenever more
    points are in range than were asked for. And on PostgreSQL it is what lets the GiST index
    prune: ``ST_DWithin`` is index-assisted, a distance computed per row and compared afterwards
    is not.
    """
    capped = max(1, min(int(limit), MAX_RESULTS))
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        return _nearest_postgis(db, floor_id, x, y, capped, within_px)
    return _nearest_portable(db, floor_id, x, y, capped, within_px)


def _nearest_postgis(
    db: Session, floor_id: str, x: float, y: float, limit: int, within_px: float | None
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
    statement = select(CaptureLocation, cast(distance, Float).label("distance")).where(
        CaptureLocation.floor_id == floor_id
    )
    if within_px is not None:
        statement = statement.where(func.ST_DWithin(geom, point, within_px))
    statement = statement.order_by(distance).limit(limit)
    return [(row[0], float(row[1])) for row in db.execute(statement)]


def _nearest_portable(
    db: Session, floor_id: str, x: float, y: float, limit: int, within_px: float | None
) -> list[tuple[CaptureLocation, float]]:
    """Exact nearest neighbour without a spatial index.

    Squared distance throughout, with the square root taken in Python: ordering by the square is
    equivalent, comparing against a squared radius is equivalent, and SQLite's ``sqrt`` exists
    only in builds compiled with the math functions — so calling it in SQL would work on one
    machine and raise ``no such function`` on another.
    """
    dx = CaptureLocation.x - x
    dy = CaptureLocation.y - y
    squared = dx * dx + dy * dy
    statement = select(CaptureLocation, squared.label("distance_sq")).where(
        CaptureLocation.floor_id == floor_id
    )
    if within_px is not None:
        # Squaring the radius rather than rooting the distance keeps this exact: both sides are
        # non-negative, so the comparison is equivalent, and no float rounding is introduced by a
        # square root that would only be undone by the comparison.
        statement = statement.where(squared <= within_px * within_px)
    statement = statement.order_by(squared).limit(limit)
    return [(row[0], math.sqrt(max(0.0, float(row[1])))) for row in db.execute(statement)]


def captures_near(
    db: Session,
    floor_id: str,
    x: float,
    y: float,
    *,
    within_px: float,
    max_points: int = 3,
) -> list[tuple[CaptureLocation, float, list[CaptureFrame]]]:
    """Every visit to the capture points near ``(x, y)``, newest first — the "time travel" query.

    The defining question a construction viewer answers is not "what is here" but "what was here
    on the other dates", and the click that asks it lands on blank floor plan, not on a point. So
    the radius is the whole mechanism: it turns a coordinate the operator chose into the handful
    of capture points that actually saw that part of the building, and returns nothing when they
    clicked somewhere nobody walked.

    Frames for every matched point are fetched in one query rather than one per point. The list
    is short, so this is not about the row count — it is that a loop here would issue queries in
    proportion to a radius the caller supplies, which is the kind of cost that only shows up once
    somebody widens it.
    """
    found = nearest_locations(db, floor_id, x, y, limit=max_points, within_px=within_px)
    if not found:
        return []

    by_location: dict[str, list[CaptureFrame]] = {location.id: [] for location, _ in found}
    frames = db.scalars(
        select(CaptureFrame)
        .where(CaptureFrame.location_id.in_(by_location))
        .order_by(CaptureFrame.captured_at.desc())
    )
    for frame in frames:
        # `location_id` cannot be None here: it is what the IN clause matched on.
        by_location[str(frame.location_id)].append(frame)

    return [(location, distance, by_location[location.id]) for location, distance in found]
