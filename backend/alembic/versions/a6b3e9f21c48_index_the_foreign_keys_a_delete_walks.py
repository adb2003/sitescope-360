"""Index the foreign keys a delete walks

On PostgreSQL an unindexed foreign key makes every delete of the *parent* scan the whole child
table, because that is the only way to find the rows whose reference has to be cascaded or set
null. Deleting one zone would read every capture session in the deployment; deleting one
uploaded file would read every processing job ever run.

Six columns, each because something real can delete its parent:

* ``capture_locations.floor_plan_id`` — earns it twice over. It is filtered directly, since
  deleting a plan first asks whether any capture point still references it, and it is a SET NULL
  target when one goes.
* ``capture_locations.zone_id`` and ``capture_sessions.zone_id`` — a zone is deleted with the
  floor it belongs to, and both tables grow with use.
* ``capture_sessions.building_id`` and ``capture_sessions.camera_device_id`` — the same, for a
  building and a registered camera.
* ``processing_jobs.file_id`` — ON DELETE CASCADE on a table that gains a row per stage per
  capture, so seven per walkthrough and climbing.

**Fourteen other unindexed foreign keys were left alone deliberately**, and the reason is worth
recording because it is not obvious from the schema: a user is never deleted. ``DELETE
/users/{id}`` deactivates, so captures keep their operator attribution — which means every
``operator_id``, ``created_by_id``, ``assigned_to_id``, ``uploaded_by_id`` and
``requested_by_id`` SET NULL can never fire. An index there would be maintained on every write
for a cascade that cannot happen. The rest sit on tables small enough that a scan is cheaper
than the index: schedules, camera devices, project members.

Indexes only. Nothing about what is stored changes, so there is no backfill and no data risk;
on a large PostgreSQL deployment these are the one kind of migration worth running
CONCURRENTLY by hand instead.

Revision ID: a6b3e9f21c48
Revises: f2d81a45c706
Create Date: 2026-10-07 11:48:02.117344
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a6b3e9f21c48"
down_revision: str | None = "f2d81a45c706"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``(index name, table, column)`` — one row per foreign key a delete has to walk.
INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_locations_floor_plan", "capture_locations", "floor_plan_id"),
    ("ix_locations_zone", "capture_locations", "zone_id"),
    ("ix_sessions_building", "capture_sessions", "building_id"),
    ("ix_sessions_zone", "capture_sessions", "zone_id"),
    ("ix_sessions_camera_device", "capture_sessions", "camera_device_id"),
    ("ix_jobs_file", "processing_jobs", "file_id"),
)


def upgrade() -> None:
    for name, table, column in INDEXES:
        op.create_index(name, table, [column])


def downgrade() -> None:
    for name, table, _column in reversed(INDEXES):
        op.drop_index(name, table_name=table)
