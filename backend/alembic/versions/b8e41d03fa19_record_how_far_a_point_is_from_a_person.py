"""Record how far a capture point is from a person

Cross-session recognition places a capture against earlier captures of the same floor, and the
points it creates then become references for the capture after that. Nothing stopped that chain
from continuing indefinitely: recognition matches to within a viewpoint tolerance rather than
exactly, so each hop can add up to about a metre, and after a few hops a point can be a room away
from where it belongs while looking exactly like a point somebody placed by hand.

``placement_generation`` makes the chain visible. ``0`` is a point a person put down, or one a
transform they anchored produced. ``1`` was created by recognising against generation-0 points,
and so on. `services/relocalization.MAX_ANCHOR_GENERATION` refuses to *create* points from
references deeper than that — but never refuses to reuse one, because joining an existing point
introduces no new coordinate and so adds no error, which is the distinction the whole column
exists to support.

Not null, defaulting to 0, and backfilled to 0 for every existing row. Unlike most backfills in
this project that is not an invention: this migration ships alongside the cap, and every point
that exists before it was created either by a person or by a hand-anchored fit. There was no
mechanism that could have produced a deeper one.

Revision ID: b8e41d03fa19
Revises: a3f52c9d1e77
Create Date: 2026-09-25 12:38:44.902117
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e41d03fa19"
down_revision: str | None = "a3f52c9d1e77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("capture_locations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "placement_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("capture_locations", schema=None) as batch_op:
        batch_op.drop_column("placement_generation")
