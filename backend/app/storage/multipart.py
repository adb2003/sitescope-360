"""Chunked, resumable uploads (§7, §24, §32).

A single presigned PUT is all-or-nothing. On a site connection that is exactly the wrong shape:
a two-gigabyte walkthrough that drops at ninety percent has to start again from zero, and the
operator who walked the floor is the one who waits. Splitting the object into parts changes the
unit of retry from *the file* to *one part*, so a drop costs seconds instead of everything.

The part numbers, sizes and limits here are not ours — they are S3's, and every S3-compatible
service implements the same ones:

* every part except the last must be at least 5 MiB;
* there may be at most 10,000 parts;
* parts are numbered from 1 and the object is the concatenation of them in that order.

They are stated once, here, so the two providers cannot drift apart on them and so the arithmetic
that derives a part plan from a file size can be tested as arithmetic.

**Who counts the parts.** The client does not report what it uploaded. It asks the server, which
enumerates what actually landed — `list_parts` on S3, a directory listing locally. That is the
difference between "resumable" as a word and as a behaviour: a client that crashed mid-upload has
no record of which parts succeeded, and the one place that does know is the storage backend. It
also removes a whole class of mistake, because a part the client *believes* it sent cannot be
completed into the object unless the bytes are really there.

What is still the client's to state is how many parts the file *should* have. The server checks
the landed parts against that number and refuses a gap — completing an object out of parts
1, 2, 4 would otherwise produce a file that is plausible, shorter than the original, and wrong in
a way nothing downstream could detect.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.errors import ValidationError
from app.storage.base import StoredObject

#: S3 rejects a non-final part below this at *completion* time, not at upload time — so getting
#: it wrong means discovering it after the whole transfer has already been paid for.
MIN_PART_BYTES = 5 * 1024 * 1024

#: The part size to use whenever the file is small enough to allow it. Around ten seconds on a
#: poor site uplink: short enough that re-sending one is cheap, long enough that the per-part
#: request overhead stays negligible.
DEFAULT_PART_BYTES = 8 * 1024 * 1024

#: S3's hard ceiling. Beyond it the part size has to grow instead.
MAX_PARTS = 10_000

_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PartPlan:
    """How one file is to be cut up."""

    part_size: int
    part_count: int
    total_bytes: int

    def size_of(self, part_number: int) -> int:
        """Bytes in one part. The last one is short unless the file divides exactly."""
        if not 1 <= part_number <= self.part_count:
            raise ValidationError(f"Part {part_number} is not in this upload")
        if part_number < self.part_count:
            return self.part_size
        return self.total_bytes - self.part_size * (self.part_count - 1)


def plan_parts(total_bytes: int, *, part_size: int = DEFAULT_PART_BYTES) -> PartPlan:
    """Cut ``total_bytes`` into the largest number of parts the limits allow.

    Grows the part size — rather than silently truncating the upload — when the default would
    need more than :data:`MAX_PARTS`. Rounded up to a whole MiB so the number that comes back is
    one a human reading a log can recognise.
    """
    if total_bytes <= 0:
        raise ValidationError("An upload needs a positive size")
    if part_size < MIN_PART_BYTES:
        raise ValidationError(f"Part size must be at least {MIN_PART_BYTES} bytes")

    if math.ceil(total_bytes / part_size) > MAX_PARTS:
        needed = math.ceil(total_bytes / MAX_PARTS)
        part_size = math.ceil(needed / _MIB) * _MIB

    return PartPlan(
        part_size=part_size,
        part_count=math.ceil(total_bytes / part_size),
        total_bytes=total_bytes,
    )


@dataclass(frozen=True, slots=True)
class ReceivedPart:
    """One part the storage backend confirms it is holding."""

    part_number: int
    size_bytes: int


def missing_parts(received: list[ReceivedPart], part_count: int) -> list[int]:
    """Which of ``1..part_count`` have not landed, in order.

    The list is what a resuming client is told to send, and it is also the completion check: a
    non-empty result means the object would have a hole in it.
    """
    present = {part.part_number for part in received}
    return [number for number in range(1, part_count + 1) if number not in present]


@runtime_checkable
class MultipartStorage(Protocol):
    """The optional half of :class:`~app.storage.base.StorageProvider`.

    Separate because not every backend can do this. A provider without it still serves the whole
    application — the caller falls back to a single PUT — so the capability is discovered with
    ``isinstance`` rather than assumed.
    """

    def begin_multipart(self, key: str, content_type: str) -> str:
        """Start an upload and return its id. No bytes exist at ``key`` until it is completed."""
        ...

    def signed_part_url(
        self, key: str, upload_id: str, part_number: int, ttl_seconds: int | None = None
    ) -> tuple[str, dict[str, str]]:
        """A URL the client may PUT one part to, and the headers it must send."""
        ...

    def list_multipart_parts(self, key: str, upload_id: str) -> list[ReceivedPart]:
        """What has actually landed so far, ascending by part number."""
        ...

    def complete_multipart(self, key: str, upload_id: str, content_type: str) -> StoredObject:
        """Assemble the parts into the object at ``key``.

        Uses exactly the parts the backend reports, so a caller that has not checked for gaps
        gets a short object rather than an error — which is why the gap check lives in the
        service above and not here.
        """
        ...

    def abort_multipart(self, key: str, upload_id: str) -> None:
        """Discard the parts. Must be safe to call on an upload that is already gone."""
        ...
