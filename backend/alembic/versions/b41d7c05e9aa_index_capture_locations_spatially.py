"""Index capture locations spatially with PostGIS.

A capture location is a point on a floor plan: ``x``/``y`` in plan pixels, in a local Cartesian
frame with no geodetic datum. That is why the geometry gets SRID 0 rather than 4326 -- calling it
lat/lon would make every distance PostGIS returns meaningless.

The column is ``GENERATED ALWAYS AS`` the floats beside it and ``STORED``, which is the whole
point of doing it this way: no application code writes it, no backfill is needed, and it cannot
disagree with ``x``/``y`` even if a row is updated by hand. The floats stay the source of truth.

PostgreSQL only. SQLite has no PostGIS, and the nearest-location query falls back to ordering by
squared distance there -- correct, just unindexed.

Revision ID: b41d7c05e9aa
Revises: 863871e1874f
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b41d7c05e9aa"
down_revision: str | None = "863871e1874f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_capture_locations_geom"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # Fail loudly and actionably rather than leaving a half-spatial schema behind: a plain
    # `postgres` image has no PostGIS, and the error it raises on its own ("could not open
    # extension control file") says nothing about what to do next.
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    except Exception as exc:  # pragma: no cover - depends on the server image
        raise RuntimeError(
            "This migration needs the PostGIS extension, which the stock `postgres` image does "
            "not ship. Use `postgis/postgis:17-3.5-alpine` (as docker-compose.yml does), or "
            "install postgresql-17-postgis on the server."
        ) from exc

    op.execute(
        "ALTER TABLE capture_locations "
        "ADD COLUMN geom geometry(Point, 0) "
        "GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(x, y), 0)) STORED"
    )
    op.execute(f"CREATE INDEX {_INDEX} ON capture_locations USING GIST (geom)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute("ALTER TABLE capture_locations DROP COLUMN IF EXISTS geom")
    # The extension is deliberately left installed. `CREATE EXTENSION IF NOT EXISTS` may have
    # found it already there, put there by something else, and dropping it would cascade into
    # whatever that was.
