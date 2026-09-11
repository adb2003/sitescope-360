"""Record mapping method and loop closure report

Both nullable, and deliberately so. Sessions mapped before loop closure existed have no method
recorded, and `mapping_method IS NULL` is the honest representation of "unknown" -- backfilling
them with 'dead_reckoning' would be a guess about how each was produced, which is exactly the
kind of invented provenance §46 forbids.

Revision ID: 863871e1874f
Revises: c9a2e520f124
Create Date: 2026-09-11 13:13:44.375375
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "863871e1874f"
down_revision: str | None = "c9a2e520f124"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("capture_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mapping_method", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column(
                "loop_closure_report",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("capture_sessions", schema=None) as batch_op:
        batch_op.drop_column("loop_closure_report")
        batch_op.drop_column("mapping_method")
