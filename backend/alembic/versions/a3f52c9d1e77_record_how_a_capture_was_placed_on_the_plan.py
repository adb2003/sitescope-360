"""Record how a capture was placed on the plan

A capture can now be placed automatically by recognising frames from earlier captures of the
same floor, so "how did these coordinates come to exist" has a second answer and the two are not
interchangeable. Manual anchors were checked by a person looking at the drawing; a cross-session
placement was checked by a parallax threshold. A reader cannot tell them apart from the
coordinates, which is exactly the case §46 says must be recorded rather than inferred.

``placement_method`` is separate from the existing ``mapping_method``: that one says how the
shape of the walk was estimated (dead reckoning or pose-graph SLAM), this one says how that shape
came to sit on the floor plan. Either can vary without the other.

``localization_report`` keeps the evidence — which frames matched which earlier frames, their
inlier counts and residual parallax, what was rejected and why, and the fitted transform — so an
automatic placement can be argued with rather than merely trusted.

Both nullable, and no backfill. Sessions placed before this migration were placed by hand, but
nothing observed that: the ``align`` route and the floor-plan editor both write locations and
neither left a marker, so writing "manual_anchors" across the table would be inventing a record
of an event nobody witnessed.

Revision ID: a3f52c9d1e77
Revises: d7c3f1b8a204
Create Date: 2026-09-25 09:14:22.507311
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3f52c9d1e77"
down_revision: str | None = "d7c3f1b8a204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("capture_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("placement_method", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column(
                "localization_report",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("capture_sessions", schema=None) as batch_op:
        batch_op.drop_column("localization_report")
        batch_op.drop_column("placement_method")
