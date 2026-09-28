"""Creating, resolving and revoking read-only links to one view (§27).

The whole feature is a trade: a share link gives someone with no account the ability to see one
frame, which is useful precisely because it skips authentication — and is therefore a **bearer
credential**. Anyone holding it has the access. Every decision here follows from taking that
seriously rather than from pretending a long URL is a permission system.

**The token is never stored.** Only its SHA-256, so a dump of `share_links` yields no working
links. That has a consequence worth stating plainly rather than working around: the token can be
returned exactly once, when it is created. There is no "show me that link again" — there is
"make another one and revoke the old".

**A plain digest, not bcrypt.** The token is 256 bits from `secrets`, not a human-chosen
password: there is no dictionary to slow an attacker down through, and a work factor would tax
every legitimate view to defend against a search that is already infeasible.

**Expiry is mandatory and capped.** A link with no expiry is an account nobody administers, and
one lasting a year is the same thing more slowly. `MAX_TTL_DAYS` is the outer bound; the default
is deliberately shorter than someone would pick if they had to think about it.

**Resolution is uniformly not-found.** Wrong token, expired, revoked, or frame since deleted all
answer the same way. A visitor who is told "this link has expired" learns that it was once real,
which is the one thing a guesser wants to know.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import log_event
from app.models import CaptureFrame, CaptureSession, ShareLink, User

#: Bytes of randomness in a token. 32 bytes is 256 bits, which `token_urlsafe` renders as 43
#: characters — long enough that guessing is not a threat model, short enough to paste.
TOKEN_BYTES = 32

#: Default lifetime when the caller does not choose one.
#:
#: Seven days, because the question a share link answers is usually a current one — "is this slab
#: ready to sign off" — and because a default that outlives the question is how a deployment ends
#: up with live links nobody remembers making.
DEFAULT_TTL_DAYS = 7

#: The longest a link may live. Ninety days covers a defects period without becoming permanent.
MAX_TTL_DAYS = 90


def hash_token(token: str) -> str:
    """The lookup key for a token. Deterministic, so a request can find its row."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CreatedShare:
    """A new link, and the only time its token exists outside the holder's hands."""

    share: ShareLink
    token: str


def create_share(
    db: Session,
    frame: CaptureFrame,
    user: User,
    *,
    expires_in_days: int | None = None,
    label: str | None = None,
) -> CreatedShare:
    """Mint a link to one frame, returning the token to show once and never again."""
    days = int(expires_in_days or DEFAULT_TTL_DAYS)
    if days < 1 or days > MAX_TTL_DAYS:
        raise ValidationError(
            f"A share link must last between 1 and {MAX_TTL_DAYS} days",
            requested_days=days,
        )

    session = db.get(CaptureSession, frame.session_id)
    if session is None:
        raise NotFoundError("The frame's capture session no longer exists")

    token = secrets.token_urlsafe(TOKEN_BYTES)
    share = ShareLink(
        token_hash=hash_token(token),
        frame_id=frame.id,
        project_id=session.project_id,
        created_by_id=user.id,
        label=(label or None),
        expires_at=datetime.now(UTC) + timedelta(days=days),
    )
    db.add(share)
    db.flush()

    # The token is deliberately absent from this event. A log is a place a URL survives long
    # after the link it names should have stopped working, and logs are read by more people and
    # shipped to more places than the database is.
    log_event(
        "share.created",
        share_id=share.id,
        frame_id=frame.id,
        project_id=session.project_id,
        created_by=user.id,
        expires_at=share.expires_at.isoformat(),
    )
    return CreatedShare(share=share, token=token)


def resolve(db: Session, token: str) -> ShareLink:
    """Find a live link by its token, or refuse without saying which way it failed.

    Wrong, expired, revoked and deleted-frame all raise the same `NotFoundError`. Distinguishing
    them would tell a visitor holding a dead token whether it was ever a real one — the single
    most useful fact to anyone probing, and of no use to the person it was shared with, who needs
    to ask the sender for a new link either way.
    """
    share = db.scalar(select(ShareLink).where(ShareLink.token_hash == hash_token(token)))
    if share is None or not is_live(share):
        raise NotFoundError("This link is not valid")
    return share


def is_live(share: ShareLink, *, now: datetime | None = None) -> bool:
    moment = now or datetime.now(UTC)
    if share.revoked_at is not None:
        return False
    expires = share.expires_at
    if expires.tzinfo is None:
        # SQLite hands back naive datetimes; treat a stored instant as the UTC it was written as.
        expires = expires.replace(tzinfo=UTC)
    return expires > moment


def record_view(db: Session, share: ShareLink) -> None:
    """Count a view.

    Not analytics. It is how someone decides what to do about a link they are unsure of: one that
    has never been opened can be revoked without a conversation.
    """
    share.view_count = int(share.view_count or 0) + 1
    share.last_viewed_at = datetime.now(UTC)
    db.flush()


def revoke(db: Session, share: ShareLink, user: User) -> ShareLink:
    """Stop a link working now, whatever its expiry says.

    Idempotent: revoking twice is the same as once, because the second caller wants the same
    outcome and an error would only make them check whether the first attempt worked.
    """
    if share.revoked_at is None:
        share.revoked_at = datetime.now(UTC)
        db.flush()
        log_event(
            "share.revoked", share_id=share.id, project_id=share.project_id, revoked_by=user.id
        )
    return share
