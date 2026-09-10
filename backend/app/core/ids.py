"""Identifier generation.

Two flavours:

* Surrogate primary keys — UUID4 hex strings. Opaque, safe to expose, no sequence contention.
* Human-legible capture-session ids — ``CAP-20260910-103022-A8F3`` (§33). Sortable by prefix,
  readable over a radio, and unique enough for field use (the random suffix guards against two
  operators starting a session in the same second).
"""

from __future__ import annotations

import secrets
import string
import uuid
from datetime import UTC, datetime

_SUFFIX_ALPHABET = string.ascii_uppercase + string.digits


def new_id() -> str:
    """A surrogate primary key."""
    return uuid.uuid4().hex


def new_capture_session_id(when: datetime | None = None) -> str:
    """``CAP-YYYYMMDD-HHMMSS-XXXX`` — see §33."""
    moment = (when or datetime.now(UTC)).astimezone(UTC)
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(4))
    return f"CAP-{moment:%Y%m%d}-{moment:%H%M%S}-{suffix}"


def location_code(index: int) -> str:
    """``P01``, ``P02``, … ``P100``."""
    return f"P{index:02d}"
