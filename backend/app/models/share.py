"""Read-only links to a single view, for people without an account (§27).

A capture is worth showing to people who will never have a login: the client who wants to see the
slab before signing it off, the consultant asked whether a detail was built as drawn. Until now
the only answers were to give them an account on the project or to email a screenshot, and the
screenshot is what actually happens — which loses the date, the floor, the location and the
ability to look around.

A share link is a **bearer credential**, and the model is shaped around admitting that:

* the token is stored as a **SHA-256 hash**, never in the clear, so a copy of this table is not a
  set of working links — and as a consequence the token can be shown exactly once, at creation,
  which is the honest behaviour anyway;
* an **expiry is mandatory**. A link with no end is an account nobody administers;
* **revocation is immediate** and independent of expiry, because the reason to revoke is usually
  that the link went somewhere it should not have;
* the scope is **one frame**, not a project or a floor. Anyone holding the link sees that view
  and cannot walk to its neighbours, its other dates, or its annotations.

Views are counted and the last one timestamped. Not analytics: it is how someone decides whether
a link they are unsure about was ever actually used.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, PKType, TimestampMixin, UTCDateTime, pk_column


class ShareLink(Base, TimestampMixin):
    __tablename__ = "share_links"
    __table_args__ = (
        Index("ix_share_links_frame", "frame_id"),
        Index("ix_share_links_project", "project_id"),
    )

    id: Mapped[str] = pk_column()

    #: SHA-256 of the token, hex. Unique because it is what a request is looked up by.
    #:
    #: A plain digest and not bcrypt: the token is 256 bits of randomness rather than a password,
    #: so there is nothing to slow down a guesser about — brute force is already hopeless — and a
    #: work factor here would cost every legitimate view instead.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    frame_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("capture_frames.id", ondelete="CASCADE"), nullable=False
    )
    #: Denormalised from the frame's session so links can be listed and revoked per project
    #: without joining through three tables, and so a project's links die with the project.
    project_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    created_by_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: What the sharer called it — "for Meridian's QS", "slab query 14/9". Shown in the list, so
    #: revoking the right one does not mean guessing from timestamps.
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: Set when someone revokes it. Separate from expiry because the two answer different
    #: questions: expiry is how long this was meant to last, revocation is that it should stop now.
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_viewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ShareLink frame={self.frame_id} expires={self.expires_at.isoformat()}>"
