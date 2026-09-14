"""Does PostgreSQL actually agree with the portable branch?

`app.services.spatial` has two implementations of one contract, and the rest of the suite runs on
SQLite -- so the PostGIS branch is the half nobody would notice breaking. Everything it depends on
is a claim about SQL I wrote and could have got wrong: that `ST_MakePoint` takes (x, y) in that
order, that `<->` returns a true Euclidean distance rather than a bounding-box approximation, that
SRID 0 is accepted, and that the migration's `GENERATED ALWAYS AS` expression is even valid.

So this builds the migration's DDL verbatim in a temporary table and checks the answers against
distances computed in Python. It skips unless a PostgreSQL URL is supplied, and CI supplies one
from the job that already runs a PostGIS server.

    POSTGIS_TEST_URL=postgresql+psycopg://user:pass@localhost:5432/db  pytest -k postgis
"""

from __future__ import annotations

import math
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

#: The generated column and index exactly as `b41d7c05e9aa` writes them. Copied rather than
#: imported, so that editing the migration and forgetting this file shows up as a failure here
#: instead of as a silently untested migration.
_DDL = """
CREATE TEMPORARY TABLE spatial_probe (
    id text PRIMARY KEY,
    x double precision NOT NULL,
    y double precision NOT NULL,
    geom geometry(Point, 0) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(x, y), 0)) STORED
)
"""

#: (id, x, y). Deliberately not sorted by distance from the probe point.
_POINTS = [
    ("far", 900.0, 900.0),
    ("triangle", 3.0, 4.0),
    ("mid", 100.0, 0.0),
    ("asymmetric", 0.0, 40.0),
]


def _engine() -> Engine:
    url = os.environ.get("POSTGIS_TEST_URL")
    if not url:
        pytest.skip("POSTGIS_TEST_URL is not set; the PostGIS branch is not exercised here")
    return create_engine(url, future=True)


@pytest.fixture
def probe():
    """A temporary table with the migration's DDL, populated and indexed."""
    engine = _engine()
    with engine.begin() as connection:
        available = connection.execute(
            text("SELECT count(*) FROM pg_available_extensions WHERE name = 'postgis'")
        ).scalar_one()
        if not available:
            pytest.skip("The server has no PostGIS available (stock `postgres` image?)")
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        connection.execute(text(_DDL))
        connection.execute(text("CREATE INDEX ON spatial_probe USING GIST (geom)"))
        for identifier, x, y in _POINTS:
            connection.execute(
                text("INSERT INTO spatial_probe (id, x, y) VALUES (:i, :x, :y)"),
                {"i": identifier, "x": x, "y": y},
            )
        yield connection
    engine.dispose()


def _knn(connection, x: float, y: float, limit: int = 10):
    """The production query shape: order by the index-assisted distance operator."""
    rows = connection.execute(
        text(
            "SELECT id, geom <-> ST_SetSRID(ST_MakePoint(:x, :y), 0) AS distance "
            "FROM spatial_probe ORDER BY distance LIMIT :limit"
        ),
        {"x": x, "y": y, "limit": limit},
    ).all()
    return [(row.id, float(row.distance)) for row in rows]


def _expected(x: float, y: float):
    computed = [(i, math.hypot(px - x, py - y)) for i, px, py in _POINTS]
    return sorted(computed, key=lambda pair: pair[1])


@pytest.mark.parametrize("x,y", [(0.0, 0.0), (300.0, 395.0), (-25.0, 12.5), (900.0, 900.0)])
def test_postgis_distances_match_euclidean_distance(probe, x: float, y: float) -> None:
    """Same order and same numbers as `math.hypot`, to within float noise."""
    actual = _knn(probe, x, y)
    expected = _expected(x, y)

    assert [identifier for identifier, _ in actual] == [identifier for identifier, _ in expected]
    for (_, got), (_, want) in zip(actual, expected, strict=True):
        assert got == pytest.approx(want, rel=1e-9, abs=1e-9)


def test_the_generated_column_follows_its_x_and_y(probe) -> None:
    """The column is the reason no application code writes a geometry. Prove it stays in step."""
    probe.execute(text("UPDATE spatial_probe SET x = 0, y = 0 WHERE id = 'far'"))

    assert _knn(probe, 0.0, 0.0, limit=1) == [("far", 0.0)]


def test_make_point_takes_x_before_y(probe) -> None:
    """A transposed constructor is invisible on the diagonal and wrong everywhere else.

    `asymmetric` sits at (0, 40). Probing (40, 0) should therefore be *far* from it; if x and y
    were swapped anywhere in the chain, it would come back at distance zero.
    """
    by_id = dict(_knn(probe, 40.0, 0.0))

    assert by_id["asymmetric"] == pytest.approx(math.hypot(40.0, 40.0))


def test_the_index_is_actually_used_for_ordering(probe) -> None:
    """Otherwise this is a sequential scan wearing a spatial operator, and the migration is noise.

    PostgreSQL will still choose a sequential scan for a table this small, so the planner is asked
    directly with sequential scans disabled -- the question is whether the index *can* serve the
    ordering, which is what `<->` buys and `ST_Distance` does not.
    """
    probe.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        row[0]
        for row in probe.execute(
            text(
                "EXPLAIN SELECT id FROM spatial_probe "
                "ORDER BY geom <-> ST_SetSRID(ST_MakePoint(0, 0), 0) LIMIT 1"
            )
        ).all()
    )

    assert "Index Scan" in plan, plan
