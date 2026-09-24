"""Capture-session domain logic: creation, upload, frame/location binding, viewer data."""

from __future__ import annotations

import contextlib
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import BinaryIO, NoReturn, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    CaptureSessionStatus,
    DataProvenance,
    FileKind,
    ProjectionType,
)
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedMediaError,
    ValidationError,
)
from app.core.ids import location_code, new_capture_session_id, new_id
from app.core.logging import log_event
from app.models import (
    Building,
    CaptureFile,
    CaptureFrame,
    CaptureLocation,
    CaptureSession,
    Floor,
    FloorPlan,
    User,
    Zone,
)
from app.processing.media import SniffResult, sniff
from app.schemas.capture import CaptureSessionCreate
from app.storage import extension_of, get_storage, session_key, session_prefix
from app.storage.base import validate_key
from app.storage.maintenance import purge_abandoned_uploads
from app.storage.multipart import (
    MIN_PART_BYTES,
    MultipartStorage,
    PartPlan,
    missing_parts,
    plan_parts,
)


def create_session(db: Session, payload: CaptureSessionCreate, operator: User) -> CaptureSession:
    """Create a capture session, resolving the project/building chain from the floor (§33)."""
    floor = db.get(Floor, payload.floor_id)
    if floor is None:
        raise NotFoundError(f"Floor {payload.floor_id} not found")
    building = db.get(Building, floor.building_id)
    if building is None:
        raise NotFoundError("The floor's building no longer exists")

    if payload.zone_id:
        zone = db.get(Zone, payload.zone_id)
        if zone is None or zone.floor_id != floor.id:
            raise ValidationError("That zone does not belong to the selected floor")

    started_at = payload.started_at or datetime.now(UTC)

    # Idempotent create for offline mobile clients (§32): a retried request that carries the id
    # the phone already generated returns the existing session instead of a duplicate.
    session_id = payload.client_session_id or new_capture_session_id(started_at)
    existing = db.get(CaptureSession, session_id)
    if existing is not None:
        if existing.floor_id != floor.id:
            raise ConflictError(
                f"Capture session id {session_id} already exists on a different floor"
            )
        return existing

    session = CaptureSession(
        id=session_id,
        project_id=building.project_id,
        building_id=building.id,
        floor_id=floor.id,
        zone_id=payload.zone_id,
        name=payload.name,
        status=CaptureSessionStatus.DRAFT,
        started_at=started_at,
        operator_id=operator.id,
        capture_source=payload.capture_source,
        camera_device_id=payload.camera_device_id,
        camera_label=payload.camera_label,
        frame_interval_seconds=payload.frame_interval_seconds,
        notes=payload.notes,
        gps_latitude=payload.gps_latitude,
        gps_longitude=payload.gps_longitude,
    )
    db.add(session)
    db.commit()
    log_event(
        "capture.created",
        session_id=session.id,
        project_id=session.project_id,
        floor_id=session.floor_id,
        operator_id=operator.id,
        capture_source=str(session.capture_source),
    )
    return session


def store_upload(
    db: Session,
    session: CaptureSession,
    *,
    filename: str,
    stream: BinaryIO,
    declared_content_type: str | None = None,
) -> CaptureFile:
    """Validate and persist one uploaded file (§7, §44).

    Order matters. The extension allow-list and the magic-byte check both run *before* anything
    is written, and the size cap is enforced *while* streaming rather than by trusting a
    Content-Length header — so an oversized or disguised upload never reaches storage.
    """
    if session.status in (CaptureSessionStatus.CANCELLED,):
        raise ConflictError("This capture session was cancelled")

    safe_name = PurePosixPath(filename).name
    if not safe_name:
        raise ValidationError("Upload is missing a filename")

    extension = extension_of(safe_name)
    if extension not in settings.allowed_capture_extensions:
        raise UnsupportedMediaError(
            f"{extension or 'This file type'} is not supported. Allowed: "
            + ", ".join(settings.allowed_capture_extensions)
        )

    header = stream.read(64)
    stream.seek(0)
    sniffed = sniff(header, safe_name)
    if sniffed.kind is FileKind.UNKNOWN:
        raise UnsupportedMediaError(f"{safe_name} is not a supported image or video")

    file_id = new_id()
    key = session_key(
        session.project_id,
        session.floor_id,
        session.id,
        "source",
        f"{file_id}{extension}",
    )

    digest = hashlib.sha256()
    total = 0
    limit = settings.max_upload_bytes

    # _CountingStream forwards every other attribute to the wrapped upload, so it satisfies the
    # provider contract without inheriting from BinaryIO (which is a Protocol-shaped ABC that
    # cannot be usefully subclassed here).
    counting = cast("BinaryIO", _CountingStream(stream, digest, limit))
    storage = get_storage()
    try:
        stored = storage.put(key, counting, sniffed.content_type)
    except PayloadTooLargeError:
        # Remove the partial object so a rejected upload leaves nothing behind.
        with contextlib.suppress(Exception):
            storage.delete(key)
        raise
    total = stored.size_bytes
    checksum = digest.hexdigest()

    duplicate = db.scalar(
        select(CaptureFile).where(
            CaptureFile.session_id == session.id,
            CaptureFile.checksum_sha256 == checksum,
        )
    )
    if duplicate is not None:
        # A retried mobile upload of the same bytes: keep the original record, drop the new copy.
        with contextlib.suppress(Exception):
            storage.delete(key)
        return duplicate

    return _record_file(
        db,
        session,
        file_id=file_id,
        safe_name=safe_name,
        key=key,
        sniffed=sniffed,
        size_bytes=total,
        checksum=checksum,
        via="multipart",
    )


@dataclass(frozen=True, slots=True)
class DirectUploadTicket:
    """Everything a client needs to put one object into storage without the API in the middle."""

    storage_key: str
    upload_url: str
    headers: dict[str, str]
    expires_in: int


#: Enough bytes for every container signature `sniff` knows about.
_SNIFF_BYTES = 64

#: The shape of a key `new_id` mints: a uuid4 hex, lower case, no separators.
_MINTED_ID = re.compile(r"[0-9a-f]{32}")


def _source_key(session: CaptureSession, file_id: str, extension: str) -> str:
    return session_key(
        session.project_id, session.floor_id, session.id, "source", f"{file_id}{extension}"
    )


def _checked_filename(filename: str) -> tuple[str, str]:
    """The shared front half of both upload paths: a safe name and an allowed extension."""
    safe_name = PurePosixPath(filename).name
    if not safe_name:
        raise ValidationError("Upload is missing a filename")
    extension = extension_of(safe_name)
    if extension not in settings.allowed_capture_extensions:
        raise UnsupportedMediaError(
            f"{extension or 'This file type'} is not supported. Allowed: "
            + ", ".join(settings.allowed_capture_extensions)
        )
    return safe_name, extension


def _checked_source_key(
    session: CaptureSession, storage_key: str, safe_name: str, extension: str
) -> None:
    """Confirm a client-supplied key is one *this* session could have been issued.

    Shared by both client-writes-first routes, because the check is the same and getting it
    wrong in only one of them would be the easiest possible thing to miss.
    """
    validate_key(storage_key)

    # Without this a client could name another session's object -- or one of its own *frames* --
    # as a source file, and the server would happily adopt it.
    prefix = session_prefix(session.project_id, session.floor_id, session.id, "source")
    if not storage_key.startswith(prefix + "/"):
        raise ValidationError("That storage key does not belong to this capture session")

    # The key was minted from this filename's extension, so a mismatch means the client is
    # naming a different file than it asked for. Pipeline stages stage temp files by the key's
    # suffix, so letting the two disagree hands a .mp4 to an image decoder.
    if PurePosixPath(storage_key).suffix.lower() != extension:
        raise ValidationError(
            f"{safe_name} does not match the uploaded object's type ({storage_key})"
        )


def prepare_direct_upload(
    db: Session,
    session: CaptureSession,
    *,
    filename: str,
    content_type: str | None = None,
) -> DirectUploadTicket:
    """Issue a signed URL the client PUTs bytes to, bypassing the API (§7, §24).

    The alternative to :func:`store_upload`, not a replacement for it. A 4 GB walkthrough video
    streamed through the API occupies a worker for the whole transfer; this hands the bytes
    straight to object storage instead. Small panorama uploads are better off on the proxied
    path, which validates before anything is stored.

    The key is generated here, never accepted from the client. A client-named key is a
    client-chosen write target, and no signature makes that safe.
    """
    if session.status is CaptureSessionStatus.CANCELLED:
        raise ConflictError("This capture session was cancelled")

    safe_name, extension = _checked_filename(filename)
    file_id = new_id()
    key = _source_key(session, file_id, extension)

    storage = get_storage()
    issue = getattr(storage, "signed_upload_url", None)
    if issue is None:
        raise ValidationError(
            f"The {storage.name} storage backend does not support direct uploads; "
            "POST the file to /captures/{id}/upload instead."
        )
    url, headers = issue(key, content_type or "application/octet-stream")

    log_event("upload.url_issued", session_id=session.id, storage_key=key, filename=safe_name)
    return DirectUploadTicket(
        storage_key=key,
        upload_url=url,
        headers=headers,
        expires_in=settings.storage_url_ttl_seconds,
    )


@dataclass(frozen=True, slots=True)
class PartTicket:
    """One part's URL. Only parts the client still has to send get one."""

    part_number: int
    upload_url: str
    headers: dict[str, str]
    size_bytes: int


@dataclass(frozen=True, slots=True)
class MultipartTicket:
    """A whole chunked upload, as the client needs to see it.

    ``received`` is the resumable half: it is what storage confirms it is already holding, so a
    client coming back after a crash knows what not to send again. ``parts`` is the complement —
    the work that remains — which is why the two together are always the full plan.
    """

    storage_key: str
    upload_id: str
    part_size: int
    part_count: int
    total_bytes: int
    received: list[int]
    parts: list[PartTicket]
    expires_in: int


def _multipart_storage() -> MultipartStorage:
    storage = get_storage()
    if not isinstance(storage, MultipartStorage):
        raise ValidationError(
            f"The {storage.name} storage backend does not support chunked uploads; "
            "request a single upload URL instead."
        )
    return storage


def _ticket(
    storage: MultipartStorage,
    *,
    storage_key: str,
    upload_id: str,
    plan: PartPlan,
    wanted: list[int],
    received: list[int],
) -> MultipartTicket:
    parts: list[PartTicket] = []
    for number in wanted:
        url, headers = storage.signed_part_url(storage_key, upload_id, number)
        parts.append(
            PartTicket(
                part_number=number,
                upload_url=url,
                headers=headers,
                size_bytes=plan.size_of(number),
            )
        )

    return MultipartTicket(
        storage_key=storage_key,
        upload_id=upload_id,
        part_size=plan.part_size,
        part_count=plan.part_count,
        total_bytes=plan.total_bytes,
        received=received,
        parts=parts,
        expires_in=settings.storage_url_ttl_seconds,
    )


def prepare_multipart_upload(
    db: Session,
    session: CaptureSession,
    *,
    filename: str,
    size_bytes: int,
    content_type: str | None = None,
) -> MultipartTicket:
    """Start a chunked upload and hand back a URL for every part (§7, §32).

    The third upload route, and the one for the case the other two handle badly. A single PUT of
    a two-gigabyte walkthrough is one unit of retry: if it drops at ninety percent, ninety
    percent is what has to be sent again, over the connection that just proved it cannot hold.
    Here each part is its own PUT, so a drop costs one part.

    The size is required rather than inferred because the part plan has to exist before the
    first byte moves — both ends must agree on where the cut points are, or part 3 from the
    client is not the range the server will assemble as part 3. It is also the earliest possible
    place to refuse an oversized upload: after the transfer, all that can be done is delete it.
    """
    if session.status is CaptureSessionStatus.CANCELLED:
        raise ConflictError("This capture session was cancelled")
    if size_bytes > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            f"Upload exceeds the {settings.max_upload_bytes / (1024**3):.1f} GiB limit"
        )

    safe_name, extension = _checked_filename(filename)
    storage = _multipart_storage()

    # Opportunistic, throttled to once an hour, and deliberately here: starting a chunked upload
    # is the act that creates parts, so it is the right moment to collect the ones an earlier
    # attempt abandoned. A busy deployment sweeps often and an idle one does not spin, with no
    # scheduler process to run.
    purge_abandoned_uploads()

    plan = plan_parts(size_bytes)
    key = _source_key(session, new_id(), extension)
    upload_id = storage.begin_multipart(key, content_type or "application/octet-stream")

    log_event(
        "upload.multipart_started",
        session_id=session.id,
        storage_key=key,
        filename=safe_name,
        parts=plan.part_count,
        part_size=plan.part_size,
    )
    return _ticket(
        storage,
        storage_key=key,
        upload_id=upload_id,
        plan=plan,
        wanted=list(range(1, plan.part_count + 1)),
        received=[],
    )


def resume_multipart_upload(
    session: CaptureSession,
    *,
    storage_key: str,
    upload_id: str,
    filename: str,
    size_bytes: int,
) -> MultipartTicket:
    """Ask what landed, and get fresh URLs for what did not.

    Two things expire during a long upload and both end here. The obvious one is the client:
    an app killed mid-transfer has no record of which parts succeeded, and the only component
    that does is the storage backend. The less obvious one is the URLs themselves — a signed URL
    issued for part 200 has a lifetime, and a slow site connection can outlast it, so a resume
    that only skipped completed parts would still stall on expired links for the rest.
    """
    safe_name, extension = _checked_filename(filename)
    _checked_source_key(session, storage_key, safe_name, extension)

    storage = _multipart_storage()
    plan = plan_parts(size_bytes)
    received = storage.list_multipart_parts(storage_key, upload_id)

    return _ticket(
        storage,
        storage_key=storage_key,
        upload_id=upload_id,
        plan=plan,
        wanted=missing_parts(received, plan.part_count),
        received=[part.part_number for part in received],
    )


def complete_multipart_upload(
    db: Session,
    session: CaptureSession,
    *,
    storage_key: str,
    upload_id: str,
    filename: str,
    size_bytes: int,
) -> CaptureFile:
    """Assemble the parts into one object, then adopt it exactly as a direct upload.

    The checks here are about the *shape* of what was uploaded; the checks about its content are
    :func:`register_direct_upload`'s, and running them a second time would be duplication with
    two places to drift. So this establishes only what that function cannot see — that the parts
    form a complete run — and then hands over.

    A gap is refused rather than assembled. S3 completes an upload out of whatever parts it is
    given, so parts 1, 2 and 4 would produce a real object: shorter than the original, decodable
    for the first two thirds, and wrong in a way no size check or magic-byte sniff would catch.
    """
    if session.status is CaptureSessionStatus.CANCELLED:
        raise ConflictError("This capture session was cancelled")

    safe_name, extension = _checked_filename(filename)
    _checked_source_key(session, storage_key, safe_name, extension)

    storage = _multipart_storage()
    plan = plan_parts(size_bytes)
    received = storage.list_multipart_parts(storage_key, upload_id)

    absent = missing_parts(received, plan.part_count)
    if absent:
        shown = ", ".join(str(number) for number in absent[:10])
        more = f" and {len(absent) - 10} more" if len(absent) > 10 else ""
        raise ConflictError(
            f"{len(absent)} of {plan.part_count} parts have not been uploaded: {shown}{more}"
        )

    # Caught here rather than at completion because S3 raises `EntityTooSmall` for a short
    # non-final part -- after the entire transfer has been paid for, and with a message that
    # says nothing about which part or why.
    undersized = [
        part.part_number
        for part in received
        if part.part_number < plan.part_count and part.size_bytes < MIN_PART_BYTES
    ]
    if undersized:
        raise ValidationError(
            f"Parts {undersized[:10]} are below the {MIN_PART_BYTES // (1024 * 1024)} MiB "
            "minimum; every part but the last must reach it"
        )

    stored = storage.complete_multipart(
        storage_key, upload_id, content_type="application/octet-stream"
    )
    log_event(
        "upload.multipart_completed",
        session_id=session.id,
        storage_key=storage_key,
        parts=plan.part_count,
        size_bytes=stored.size_bytes,
    )
    return register_direct_upload(db, session, storage_key=storage_key, filename=safe_name)


def abort_multipart_upload(session: CaptureSession, *, storage_key: str, upload_id: str) -> None:
    """Throw away a chunked upload the client has given up on.

    Worth an endpoint of its own because the parts are billable and invisible: they are not
    objects, so they do not appear in a listing of the bucket, and nothing else in this
    application would ever go looking for them.
    """
    validate_key(storage_key)
    prefix = session_prefix(session.project_id, session.floor_id, session.id, "source")
    if not storage_key.startswith(prefix + "/"):
        raise ValidationError("That storage key does not belong to this capture session")

    _multipart_storage().abort_multipart(storage_key, upload_id)
    log_event("upload.multipart_aborted", session_id=session.id, storage_key=storage_key)


def register_direct_upload(
    db: Session,
    session: CaptureSession,
    *,
    storage_key: str,
    filename: str,
) -> CaptureFile:
    """Take ownership of an object the client uploaded directly, or delete it.

    This is where the guarantee that :func:`store_upload` gets for free has to be re-established
    by hand. There the extension check, the magic-byte sniff and the size cap all run *before*
    the bytes are stored, so a rejected upload never becomes an object. Here the bytes are
    already there, so the same three checks run against the stored object and anything that
    fails is deleted — which leaves the same end state, just later and at the cost of the
    storage the attempt used.

    Note what is *not* re-established: a direct upload has no content checksum, because
    computing one means reading the whole object back through the API and that is the cost the
    caller chose this path to avoid. Retries are idempotent by key instead — a client that PUTs
    again writes the same key — so the same file registered twice is one row, but two uploads of
    identical bytes under different keys are two rows, where the proxied path would collapse
    them. `checksum_sha256` is left null rather than filled with something unverified.
    """
    if session.status is CaptureSessionStatus.CANCELLED:
        raise ConflictError("This capture session was cancelled")

    safe_name, extension = _checked_filename(filename)
    _checked_source_key(session, storage_key, safe_name, extension)

    existing = db.scalar(
        select(CaptureFile).where(
            CaptureFile.session_id == session.id,
            CaptureFile.storage_key == storage_key,
        )
    )
    if existing is not None:
        # A retried registration, which is the normal case on a flaky site connection.
        return existing

    storage = get_storage()
    if not storage.exists(storage_key):
        raise NotFoundError(
            "No object was uploaded to that key. PUT the file to the signed URL first."
        )

    def _reject(error: Exception) -> NoReturn:
        """Delete first, then raise: a rejected upload must not stay billable or reachable."""
        with contextlib.suppress(Exception):
            storage.delete(storage_key)
        raise error

    size = storage.size(storage_key)
    if size <= 0:
        _reject(ValidationError("The uploaded object is empty"))
    if size > settings.max_upload_bytes:
        _reject(
            PayloadTooLargeError(
                f"Upload exceeds the {settings.max_upload_bytes / (1024**3):.1f} GiB limit"
            )
        )

    # `sniff` signals a mismatch by raising, not by returning UNKNOWN, so it has to be caught:
    # letting the exception travel straight out returns a correct 415 to the client and leaves
    # the rejected object sitting in storage. Both outcomes are checked in the tests.
    try:
        sniffed = sniff(storage.read_range(storage_key, _SNIFF_BYTES), safe_name)
    except (UnsupportedMediaError, ValidationError) as exc:
        _reject(exc)
    if sniffed.kind is FileKind.UNKNOWN:
        _reject(UnsupportedMediaError(f"{safe_name} is not a supported image or video"))

    # The row's primary key is the key's stem, as on the proxied path. That is only safe while
    # the stem is something the server minted, so require the shape `new_id` produces rather
    # than trusting that a client could not have reached this line with a key of its own choosing.
    file_id = PurePosixPath(storage_key).stem
    if not _MINTED_ID.fullmatch(file_id):
        _reject(ValidationError("That storage key was not issued by this server"))

    return _record_file(
        db,
        session,
        file_id=file_id,
        safe_name=safe_name,
        key=storage_key,
        sniffed=sniffed,
        size_bytes=size,
        checksum=None,
        via="direct",
    )


def _record_file(
    db: Session,
    session: CaptureSession,
    *,
    file_id: str,
    safe_name: str,
    key: str,
    sniffed: SniffResult,
    size_bytes: int,
    checksum: str | None,
    via: str,
) -> CaptureFile:
    """Create the ``CaptureFile`` row and move the session to UPLOADED.

    Shared by both upload paths deliberately. The two differ in how the bytes arrive and in
    nothing else, and the moment they differ in what gets *recorded* is the moment a capture
    behaves differently downstream depending on how it was sent.
    """
    capture_file = CaptureFile(
        id=file_id,
        session_id=session.id,
        original_filename=safe_name,
        storage_key=key,
        content_type=sniffed.content_type,
        file_size_bytes=size_bytes,
        checksum_sha256=checksum,
        kind=sniffed.kind,
        # The VALIDATE stage decides the projection by inspecting pixels; at upload time all we
        # honestly know is the container format.
        projection=ProjectionType.UNKNOWN,
    )
    db.add(capture_file)
    session.status = CaptureSessionStatus.UPLOADED
    db.flush()
    session.file_count = int(
        db.scalar(select(func.count(CaptureFile.id)).where(CaptureFile.session_id == session.id))
        or 0
    )
    db.commit()

    log_event(
        "file.uploaded",
        session_id=session.id,
        file_id=capture_file.id,
        filename=safe_name,
        size_bytes=size_bytes,
        content_type=capture_file.content_type,
        via=via,
    )
    return capture_file


class _CountingStream:
    """Wraps an upload stream to hash it and abort past the size cap."""

    def __init__(self, inner: BinaryIO, digest: object, limit: int) -> None:
        self._inner = inner
        self._digest = digest
        self._limit = limit
        self._total = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._inner.read(size)
        if chunk:
            self._total += len(chunk)
            if self._total > self._limit:
                raise PayloadTooLargeError(
                    f"Upload exceeds the {self._limit / (1024**3):.1f} GiB limit"
                )
            self._digest.update(chunk)  # type: ignore[attr-defined]
        return chunk

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


# ---- location binding --------------------------------------------------------
def next_location_code(db: Session, floor_id: str) -> str:
    """``P01``, ``P02``, … the first code not already used on this floor.

    Reuses gaps left by deletions so codes stay short and dense, which matters because operators
    read them aloud.
    """
    used = set(db.scalars(select(CaptureLocation.code).where(CaptureLocation.floor_id == floor_id)))
    for index in range(1, 1000):
        candidate = location_code(index)
        if candidate not in used:
            return candidate
    raise ValidationError("This floor already has 999 capture locations")


def create_location(
    db: Session,
    floor: Floor,
    *,
    x: float,
    y: float,
    code: str | None = None,
    label: str | None = None,
    zone_id: str | None = None,
    floor_plan_id: str | None = None,
    provenance: DataProvenance = DataProvenance.MANUAL,
    confidence: float | None = None,
    created_by_id: str | None = None,
) -> CaptureLocation:
    plan = _resolve_plan(db, floor, floor_plan_id)
    # Coordinates are floor-plan pixels; a point outside the raster is a client bug, and
    # silently clamping it would hide a broken editor.
    if plan is not None and not (0 <= x <= plan.width_px and 0 <= y <= plan.height_px):
        raise ValidationError(
            "Coordinates fall outside the floor plan",
            floor_plan_size=[plan.width_px, plan.height_px],
            given=[x, y],
        )

    if zone_id:
        zone = db.get(Zone, zone_id)
        if zone is None or zone.floor_id != floor.id:
            raise ValidationError("That zone does not belong to this floor")

    resolved_code = code or next_location_code(db, floor.id)
    if db.scalar(
        select(CaptureLocation.id).where(
            CaptureLocation.floor_id == floor.id, CaptureLocation.code == resolved_code
        )
    ):
        raise ConflictError(f"Location {resolved_code} already exists on this floor")

    location = CaptureLocation(
        floor_id=floor.id,
        zone_id=zone_id,
        floor_plan_id=plan.id if plan else None,
        code=resolved_code,
        label=label,
        x=x,
        y=y,
        provenance=provenance,
        confidence=confidence,
        created_by_id=created_by_id,
    )
    db.add(location)
    db.commit()
    return location


def _resolve_plan(db: Session, floor: Floor, floor_plan_id: str | None) -> FloorPlan | None:
    if floor_plan_id:
        plan = db.get(FloorPlan, floor_plan_id)
        if plan is None or plan.floor_id != floor.id:
            raise ValidationError("That floor plan does not belong to this floor")
        return plan
    return db.scalar(
        select(FloorPlan)
        .where(FloorPlan.floor_id == floor.id, FloorPlan.is_active.is_(True))
        .order_by(FloorPlan.created_at.desc())
        .limit(1)
    )


def assign_frame_to_location(
    db: Session, frame: CaptureFrame, location: CaptureLocation
) -> CaptureFrame:
    """Bind a frame to a floor-plan location and rebuild the session's path (§12, §13)."""
    session = db.get(CaptureSession, frame.session_id)
    if session is None:
        raise NotFoundError("The frame's capture session no longer exists")
    if location.floor_id != session.floor_id:
        raise ValidationError(
            "A frame can only be placed on the floor its capture session belongs to"
        )

    frame.location_id = location.id
    frame.position_provenance = DataProvenance.MANUAL
    frame.position_confidence = 1.0
    db.flush()

    from app.processing.stages import build_capture_path

    build_capture_path(db, session)
    db.commit()
    log_event(
        "capture.frame_located",
        session_id=session.id,
        frame_id=frame.id,
        location_id=location.id,
        code=location.code,
    )
    return frame


@dataclass(frozen=True, slots=True)
class LocationStats:
    """Per-location capture roll-up.

    A typed value rather than a ``dict[str, object]``: the callers feed these straight into a
    response model, and an untyped dict forced every field through a cast or a type-ignore at
    the point where correctness actually mattered.
    """

    frame_count: int = 0
    latest_frame_id: str | None = None
    latest_captured_at: datetime | None = None


EMPTY_LOCATION_STATS = LocationStats()


def location_stats(db: Session, floor_id: str) -> dict[str, LocationStats]:
    """``{location_id: LocationStats}`` for every visited location on a floor.

    Two grouped queries for the whole floor rather than per-point lookups, so rendering 60
    capture points on a plan does not become 120 round trips (§35).
    """
    rows = db.execute(
        select(
            CaptureFrame.location_id,
            func.count(CaptureFrame.id),
            func.max(CaptureFrame.captured_at),
        )
        .join(CaptureLocation, CaptureLocation.id == CaptureFrame.location_id)
        .where(CaptureLocation.floor_id == floor_id)
        .group_by(CaptureFrame.location_id)
    ).all()
    if not rows:
        return {}

    # Resolve the newest frame per location in one pass instead of a query per location.
    newest: dict[str, str] = {}
    latest_rows = db.execute(
        select(CaptureFrame.location_id, CaptureFrame.id, CaptureFrame.captured_at)
        .join(CaptureLocation, CaptureLocation.id == CaptureFrame.location_id)
        .where(CaptureLocation.floor_id == floor_id)
        .order_by(CaptureFrame.location_id, CaptureFrame.captured_at.desc())
    ).all()
    for location_id, frame_id, _captured_at in latest_rows:
        newest.setdefault(location_id, frame_id)

    return {
        str(location_id): LocationStats(
            frame_count=int(count),
            latest_frame_id=newest.get(location_id),
            latest_captured_at=latest,
        )
        for location_id, count, latest in rows
    }
