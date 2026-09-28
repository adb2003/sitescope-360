"""Who can see which project

Roles said what a user could do and nothing said which projects they could do it to, so every
authenticated account could read every project in the deployment. A subcontractor's viewer could
open the main contractor's other jobs, and no role change fixed it: "viewer" was never meant to
mean "viewer of everything".

``project_members`` is the missing half. It is not a second role — the global role still decides
capabilities, this decides which projects those capabilities reach.

**The backfill grants every existing user access to every existing project, deliberately.**

That looks like it defeats the point, and it is the only honest option. Nobody has ever recorded
who should see what, because there was nowhere to record it; any narrower rule would be this
migration inventing an access policy out of `created_by_id` or role and enforcing it on a live
deployment. The alternatives are worse in both directions: guess and someone loses access to
their own job mid-shift with no warning and no audit trail, or grant nobody and every non-admin
is locked out the moment the service restarts.

So the migration preserves exactly the access that exists today and changes nothing for anyone.
Enforcement is real from here on — a project created after this point has the members it is
given, and an administrator can remove the backfilled rows as soon as they know who belongs
where. That is a decision for someone who knows the deployment, which is precisely why it is not
made here.

Revision ID: c5b90e2d7a41
Revises: b8e41d03fa19
Create Date: 2026-09-28 09:12:40.338712
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5b90e2d7a41"
down_revision: str | None = "b8e41d03fa19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_members",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("added_by_id", sa.String(length=32), nullable=True),
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
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )
    op.create_index("ix_project_members_project_id", "project_members", ["project_id"])
    op.create_index("ix_project_members_user", "project_members", ["user_id"])

    # Every existing user, every existing project. `added_by_id` is left null because nobody
    # granted this — it is the access that already existed, written down for the first time.
    #
    # The id is generated in SQL rather than in Python so the backfill is one statement on any
    # size of table. `hex(randomblob(16))` on SQLite and `replace(gen_random_uuid()::text,'-','')`
    # on PostgreSQL both yield the 32-character lower-case hex this schema uses elsewhere.
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        new_id = "replace(gen_random_uuid()::text, '-', '')"
        now = "now()"
    else:
        new_id = "lower(hex(randomblob(16)))"
        now = "CURRENT_TIMESTAMP"

    op.execute(
        sa.text(
            f"""
            INSERT INTO project_members (id, project_id, user_id, added_by_id,
                                         created_at, updated_at)
            SELECT {new_id}, p.id, u.id, NULL, {now}, {now}
            FROM projects p CROSS JOIN users u
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_project_members_user", table_name="project_members")
    op.drop_index("ix_project_members_project_id", table_name="project_members")
    op.drop_table("project_members")
