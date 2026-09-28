"""Read-only links to a single view

A capture is worth showing to people who will never have a login — the client signing off a
slab, the consultant asked whether a detail was built as drawn. The alternatives were giving
them an account on the project or emailing a screenshot, and the screenshot is what actually
happens, which throws away the date, the floor, the location and the ability to look around.

A share link is a bearer credential and the table admits it: the token is stored as a SHA-256
hash so a copy of this table is not a set of working links, expiry is NOT NULL because a link
with no end is an account nobody administers, and revocation is a separate column from expiry
because the two answer different questions — how long this was meant to last, versus stop now.

No backfill, because nothing was shared before this existed.

Revision ID: 04f217340694
Revises: c5b90e2d7a41
Create Date: 2026-09-28 14:11:39.963634
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "04f217340694"
down_revision: str | None = "c5b90e2d7a41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "share_links",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("frame_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("created_by_id", sa.String(length=32), nullable=True),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("view_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["frame_id"], ["capture_frames.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("share_links", schema=None) as batch_op:
        batch_op.create_index("ix_share_links_frame", ["frame_id"], unique=False)
        batch_op.create_index("ix_share_links_project", ["project_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_share_links_token_hash"), ["token_hash"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("share_links", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_share_links_token_hash"))
        batch_op.drop_index("ix_share_links_project")
        batch_op.drop_index("ix_share_links_frame")

    op.drop_table("share_links")
