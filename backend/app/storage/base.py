"""Storage abstraction (§24).

PostgreSQL holds metadata and a *key*; the bytes live behind a ``StorageProvider``. Keys are
self-describing so a project's objects can be prefix-scanned or lifecycle-managed:

    projects/{project_id}/floors/{floor_id}/sessions/{session_id}/{kind}/{name}
    projects/{project_id}/floors/{floor_id}/floorplans/{name}
    projects/{project_id}/reports/{name}
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, BinaryIO, Protocol, runtime_checkable

from app.core.errors import ValidationError

#: Only these characters may appear in a key segment. Anything else is a rejected key, which is
#: how path traversal is prevented at the abstraction boundary rather than in each provider (§44).
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._\-]+$")


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    content_type: str


def build_key(*segments: str) -> str:
    """Join and validate key segments.

    Rejects empty segments, ``.``/``..``, separators inside a segment, and any character outside
    the safe set — so a caller can never construct a key that escapes the storage root.
    """
    cleaned: list[str] = []
    for segment in segments:
        if not segment:
            raise ValidationError("Empty storage key segment")
        if segment in (".", ".."):
            raise ValidationError("Relative storage key segment is not allowed")
        if not _SAFE_SEGMENT.match(segment):
            raise ValidationError(f"Unsafe storage key segment: {segment!r}")
        cleaned.append(segment)
    return posixpath.join(*cleaned)


def validate_key(key: str) -> str:
    """Validate a key that already exists as a string (e.g. read back from the database).

    Segments are split on ``/`` directly rather than via ``PurePosixPath.parts``, because that
    property *normalises*: it turns ``a/./b`` into ``('a', 'b')`` and hides the ``.`` entirely.
    A non-canonical key is worth rejecting even when it resolves harmlessly — two spellings of
    one key break de-duplication and prefix deletion.
    """
    if not key or key.startswith("/") or "\\" in key:
        raise ValidationError("Invalid storage key")
    segments = key.split("/")
    if not segments:
        raise ValidationError("Invalid storage key")
    for part in segments:
        if part in ("", ".", "..") or not _SAFE_SEGMENT.match(part):
            raise ValidationError(f"Invalid storage key segment: {part!r}")
    return key


def validate_prefix(prefix: str) -> str:
    """Validate a key *prefix* and return it with exactly one trailing slash.

    A prefix is not a key, and `validate_key` was being asked to approve one: a trailing slash
    splits into a final empty segment, which it rejects. Every caller wrapped the resulting
    exception in `contextlib.suppress`, so `delete_prefix` on a capture's storage raised and was
    swallowed on every call since it was written — and the function reported
    ``{"swept_prefix": True, "objects": 0}``, which reads as "swept it, there was nothing there".

    The slash matters and is not merely cosmetic. On S3 a prefix match is a string match, so
    ``projects/ab`` would also delete ``projects/abcdef``; appending it is the difference between
    one project's objects and a neighbouring project's.
    """
    validate_key(prefix.rstrip("/"))
    return prefix if prefix.endswith("/") else prefix + "/"


def extension_of(filename: str) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    return suffix


@runtime_checkable
class StorageProvider(Protocol):
    """The contract every backend implements."""

    name: str

    def put(self, key: str, data: BinaryIO, content_type: str) -> StoredObject: ...

    def put_bytes(self, key: str, data: bytes, content_type: str) -> StoredObject: ...

    def open(self, key: str) -> BinaryIO: ...

    def read_bytes(self, key: str) -> bytes: ...

    def read_range(self, key: str, length: int, start: int = 0) -> bytes:
        """Up to ``length`` bytes from ``start``, without fetching the whole object.

        Exists for one reason: a direct upload has to be sniffed *after* it lands, and reading a
        two-gigabyte video back through the API to look at its first 64 bytes would undo the
        point of uploading it directly.
        """
        ...

    def exists(self, key: str) -> bool: ...

    def size(self, key: str) -> int: ...

    def delete(self, key: str) -> None: ...

    def delete_prefix(self, prefix: str) -> int: ...

    def list_keys(self, prefix: str, limit: int) -> tuple[list[str], bool]:
        """Stored keys under ``prefix``, and whether the listing was cut short.

        Exists so storage can be reconciled against the database — asking "is there anything
        here that no row points at" is otherwise unanswerable without a script written by hand,
        which is how the one leak this found was found. Capped on purpose: a bucket can hold
        millions of objects and no report is worth an unbounded scan, so the caller is told it
        was truncated rather than handed a quiet half-answer.
        """
        ...

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str: ...

    def signed_upload_url(
        self, key: str, content_type: str, ttl_seconds: int | None = None
    ) -> tuple[str, dict[str, str]]:
        """A URL the client may PUT one object to, and the headers it must send with it.

        The key is always chosen by the server, never by the client: a client-named key is a
        client-chosen write target, and no signature can make that safe.
        """
        ...

    def health(self) -> dict[str, Any]:
        """Can this backend actually be reached right now?

        Returned by ``GET /api/system/health``, which previously reported storage as the *name*
        of the configured backend — a value read from settings, which is true whether or not the
        bucket exists. A capture platform whose object store is unreachable cannot serve a single
        image or accept a single upload, and a health endpoint that answers "ok" through that is
        worse than none: it suppresses the alarm rather than raising it.

        Must never raise. An unreachable backend is a fact to report, not an exception to
        propagate out of a liveness probe — a health check that 500s tells a load balancer the
        *application* is broken when the truth is more specific and more useful.
        """
        ...

    def local_path(self, key: str) -> str | None:
        """A real filesystem path if the backend has one, else ``None``.

        Pipeline stages that must hand a path to OpenCV use this and fall back to a temporary
        file when it returns ``None``.
        """
        ...
