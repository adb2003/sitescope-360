"""Hold an account to replacing a reset password

There was no way to recover a forgotten password. `/auth/change-password` needs the current one,
and an administrator could change a user's name, role and active flag but not their credential —
so a locked-out account needed somebody with database access, and the platform had no answer at
all for the most ordinary support request there is.

It cannot be self-service here. Proving you own an address means sending something to it, and
nothing in this project may depend on a mail service to run locally. So recovery is an
administrator action, and `must_change_password` is what keeps that honest: the password they set
is a secret they know, spoken aloud or sent over whatever channel was to hand, and it is not a
password the account should keep. The flag holds the account to `/auth/me` and
`/auth/change-password` until it sets its own, so a credential somebody else chose cannot quietly
become the one protecting the account.

Checked against the row on every request rather than written into the access token. A flag set
while its holder is mid-session takes effect immediately, and no token outlives the requirement —
the same reasoning that already applies to a role change in `api/deps`.

Not null, defaulting to false, and false for every existing row. That is not an invention: no
password had been reset by an administrator before this shipped, because there was no mechanism
that could.

Revision ID: f2d81a45c706
Revises: e17a4b6c9d38
Create Date: 2026-09-30 11:32:07.415920
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2d81a45c706"
down_revision: str | None = "e17a4b6c9d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "must_change_password",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("must_change_password")
