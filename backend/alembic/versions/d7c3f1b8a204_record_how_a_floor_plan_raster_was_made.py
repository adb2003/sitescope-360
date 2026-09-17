"""Record how a floor plan raster was made

PDF sheets are now rasterised on upload instead of being stored unusable, and the page they came
from has to be persisted: capture coordinates are plan *pixels*, so a sheet set's coordinates are
meaningless without knowing which sheet they were placed on, and a raster cannot say on its own.

A column of its own rather than a key inside ``calibration``, because
``PUT /floorplans/{id}/calibration`` replaces that value wholesale -- recording the render there
would mean calibrating a plan silently erased the record of where its pixels came from.

Nullable, and no backfill. Every existing plan was uploaded as a raster, so "no render record"
is the truthful value for all of them; writing ``{"source": "upload"}`` across the table would be
inventing provenance for rows nobody observed (§46).

Revision ID: d7c3f1b8a204
Revises: b41d7c05e9aa
Create Date: 2026-09-17 09:21:03.118204
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7c3f1b8a204"
down_revision: str | None = "b41d7c05e9aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("floor_plans", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "render",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("floor_plans", schema=None) as batch_op:
        batch_op.drop_column("render")
