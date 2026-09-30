from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import Role
from app.database.base import Base, EnumType, PKType, TimestampMixin, UTCDateTime, pk_column

if TYPE_CHECKING:
    from app.models.project import Project


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = pk_column()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(EnumType(Role, 32), nullable=False, default=Role.VIEWER)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: Set when an administrator resets this account's password, cleared when the user sets
    #: their own.
    #:
    #: A reset password is a secret the administrator knows, spoken aloud or sent over whatever
    #: channel was to hand, and it is not a password the account should keep. Without this flag
    #: there is nothing to stop a temporary credential becoming a permanent one, and nothing to
    #: distinguish "this person chose this password" from "somebody else chose it for them".
    #:
    #: Enforced in `api/deps.get_current_user` rather than at sign-in, so the answer comes from
    #: the row on every request: an account flagged while its holder is mid-session stops being
    #: able to do anything but set a new password, and no token outlives the requirement.
    #: ``server_default`` as well as ``default`` because the column is NOT NULL on a table that
    #: already has rows, and false is the truthful answer for every one of them.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    managed_projects: Mapped[list[Project]] = relationship(
        "Project", back_populates="project_manager", foreign_keys="Project.project_manager_id"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email} {self.role}>"


class RefreshToken(Base, TimestampMixin):
    """Rotating refresh tokens. Only a hash is stored, so a database leak cannot mint sessions."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_user_active", "user_id", "revoked_at"),)

    id: Mapped[str] = pk_column()
    user_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
