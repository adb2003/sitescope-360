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
