"""A role per project, not per account

``project_members`` answered *which* projects a user could reach. What they could do once there
came from their account, so the role was deployment-wide: letting a subcontractor's lead run
their own site meant making them a project manager on every job they could see. This adds the
other half of the pair — the role held **on this project** — and turns the account role into a
ceiling rather than the answer.

The backfill is the whole care of this migration. Every existing row is given the account role
its user holds today, which is exactly what that membership already granted, so nobody's access
changes when it runs. A default of `viewer` would have been the safe-looking choice and would
have silently demoted every engineer and manager in the deployment the moment it was applied.

Added nullable, backfilled, then made NOT NULL, because the three steps are the only order that
works on a table with rows. `batch_alter_table` for SQLite, which rebuilds the table rather than
altering it.

Revision ID: b8d4c27e1f93
Revises: a6b3e9f21c48
Create Date: 2026-10-08 11:40:11.882104
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4c27e1f93"
down_revision: str | None = "a6b3e9f21c48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project_members", schema=None) as batch_op:
        batch_op.add_column(sa.Column("role", sa.String(length=32), nullable=True))

    # The backfill, before the column is required. Written as one UPDATE ... FROM-equivalent that
    # both SQLite and PostgreSQL accept: a correlated subquery, because `UPDATE ... FROM` is
    # spelled differently on each and this table is small on any deployment that exists.
    op.execute(
        sa.text(
            "UPDATE project_members SET role = ("
            "  SELECT users.role FROM users WHERE users.id = project_members.user_id"
            ")"
        )
    )
    # A membership whose user is gone cannot happen — the foreign key cascades — but a NULL here
    # would fail the NOT NULL below with a message naming neither the row nor the reason.
    op.execute(sa.text("UPDATE project_members SET role = 'viewer' WHERE role IS NULL"))

    with op.batch_alter_table("project_members", schema=None) as batch_op:
        batch_op.alter_column("role", existing_type=sa.String(length=32), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("project_members", schema=None) as batch_op:
        batch_op.drop_column("role")
