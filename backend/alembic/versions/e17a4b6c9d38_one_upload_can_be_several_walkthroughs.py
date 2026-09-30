"""One upload can be several walkthroughs

An operator who captures a building in a morning walks level 0, takes the lift, walks level 1,
and uploads the lot. The platform stored that as one capture session, which has exactly one
``floor_id``, and mapping estimated a single track straight through a lift ride.

Three columns make the alternative possible.

``grouping_report`` holds what walkthrough detection found during mapping: where the walk appears
to have stopped, the gap and the verified match counts either side of each break, and — for the
pairs whose frames were too featureless to judge — that it could not tell. Nullable, and null is
the truthful value for every capture processed before this shipped: the finding is a record of a
question having been asked, and it was not. It is cleared again when a capture is actually split,
because the proposal then describes a capture that no longer exists.

``split_from_session_id`` and ``split_segment_index`` record where a walkthrough came from. That
link is not cosmetic. Splitting re-points frame rows instead of copying hundreds of megabytes of
imagery, so a walkthrough's frames live under the storage prefix of the upload it came from, and
deleting that upload has to know not to sweep the prefix out from under it. The foreign key is
``ON DELETE SET NULL`` rather than cascade for the same reason a split is not a copy: a
walkthrough that outlives the record of its origin is still a walkthrough, and deleting the one
must never silently delete the other.

No backfill, and none is possible. Nothing before this migration split anything, so every
existing session is exactly what it says it is.

Revision ID: e17a4b6c9d38
Revises: 04f217340694
Create Date: 2026-09-29 11:04:18.330251
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e17a4b6c9d38"
down_revision: str | None = "04f217340694"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("capture_sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "grouping_report",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("split_from_session_id", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("split_segment_index", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_capture_sessions_split_from",
            "capture_sessions",
            ["split_from_session_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("capture_sessions", schema=None) as batch_op:
        batch_op.drop_constraint("fk_capture_sessions_split_from", type_="foreignkey")
        batch_op.drop_column("split_segment_index")
        batch_op.drop_column("split_from_session_id")
        batch_op.drop_column("grouping_report")
