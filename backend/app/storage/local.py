"""Local filesystem storage — the MVP default (§24).

Client-facing URLs are HMAC-signed and time-limited and are served by the API, so a filesystem
path is never exposed and an object cannot be fetched by guessing a key (§44).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.storage.base import StoredObject, validate_key
from app.storage.multipart import ReceivedPart

#: Upload ids come back from the client, so they are path segments under client influence. The
#: shape `uuid4().hex` produces is required rather than merely sanitised: anything else was not
#: issued here, and treating it as a directory name is how a staging prefix becomes a traversal.
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}")

#: Names the destination key an upload id belongs to. See `begin_multipart`.
_MANIFEST = "manifest.json"


class LocalStorage:
    name = "local"

    def __init__(self, root: Path | None = None, signing_secret: str | None = None) -> None:
        self.root = Path(root or settings.storage_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._secret = (signing_secret or settings.storage_signing_secret).encode()

    # ---- path resolution -------------------------------------------------
    def _resolve(self, key: str) -> Path:
        validate_key(key)
        path = (self.root / key).resolve()
        # Defence in depth: even with a validated key, confirm containment.
        if not path.is_relative_to(self.root):
            raise NotFoundError("Object not found")
        return path

    # ---- writes ----------------------------------------------------------
    def put(self, key: str, data: BinaryIO, content_type: str) -> StoredObject:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        with tmp.open("wb") as fh:
            shutil.copyfileobj(data, fh, length=1024 * 1024)
        tmp.replace(path)
        return StoredObject(key=key, size_bytes=path.stat().st_size, content_type=content_type)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> StoredObject:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return StoredObject(key=key, size_bytes=len(data), content_type=content_type)

    # ---- reads -----------------------------------------------------------
    def open(self, key: str) -> BinaryIO:
        path = self._resolve(key)
        if not path.is_file():
            raise NotFoundError(f"Object not found: {key}")
        return path.open("rb")

    def read_bytes(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise NotFoundError(f"Object not found: {key}")
        return path.read_bytes()

    def read_range(self, key: str, length: int, start: int = 0) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise NotFoundError(f"Object not found: {key}")
        with path.open("rb") as handle:
            handle.seek(start)
            return handle.read(length)

    def exists(self, key: str) -> bool:
        try:
            return self._resolve(key).is_file()
        except Exception:
            return False

    def size(self, key: str) -> int:
        path = self._resolve(key)
        if not path.is_file():
            raise NotFoundError(f"Object not found: {key}")
        return path.stat().st_size

    def local_path(self, key: str) -> str | None:
        path = self._resolve(key)
        return str(path) if path.is_file() else None

    def path_for_write(self, key: str) -> Path:
        """Where a *new* object for ``key`` belongs.

        Distinct from :meth:`local_path`, which answers "where is this object" and returns
        ``None`` when there is no file yet — exactly the case a direct upload starts from. The
        same validation and containment check applies either way, so a key still cannot escape
        the storage root.
        """
        return self._resolve(key)

    # ---- deletes ---------------------------------------------------------
    def delete(self, key: str) -> None:
        path = self._resolve(key)
        if path.is_file():
            path.unlink()

    def delete_prefix(self, prefix: str) -> int:
        base = self._resolve(prefix)
        if not base.is_dir():
            return 0
        count = sum(1 for p in base.rglob("*") if p.is_file())
        shutil.rmtree(base, ignore_errors=True)
        return count

    # ---- signed URLs -----------------------------------------------------
    def sign(
        self, key: str, ttl_seconds: int | None = None, method: str = "GET"
    ) -> tuple[int, str]:
        expires = int(time.time()) + int(ttl_seconds or settings.storage_url_ttl_seconds)
        return expires, self._signature(key, expires, method)

    def _signature(self, key: str, expires: int, method: str = "GET") -> str:
        """HMAC over the key, the expiry *and the verb*.

        The verb is what stops a read link from being replayed as a write. Asset URLs are handed
        to browsers by the hundred and end up in logs, referrers and caches; if the same bytes
        also authorised a PUT, every panorama URL would be an overwrite token for the object it
        points at. GET keeps the original two-part payload so links already issued stay valid.
        """
        payload = f"{key}:{expires}" if method == "GET" else f"{method}:{key}:{expires}"
        mac = hmac.new(self._secret, payload.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def verify(self, key: str, expires: int, signature: str, method: str = "GET") -> bool:
        if expires < int(time.time()):
            return False
        return hmac.compare_digest(self._signature(key, expires, method), signature)

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str:
        validate_key(key)
        expires, signature = self.sign(key, ttl_seconds)
        return self._url(key, expires, signature)

    def signed_upload_url(
        self, key: str, content_type: str, ttl_seconds: int | None = None
    ) -> tuple[str, dict[str, str]]:
        """A URL the client may PUT bytes to, plus the headers it must send.

        ``content_type`` is not signed here, unlike S3, because this backend re-derives the type
        from the stored bytes at registration anyway. It is still returned so that a client
        written against one provider works unchanged against the other.
        """
        validate_key(key)
        expires, signature = self.sign(key, ttl_seconds, method="PUT")
        return self._url(key, expires, signature), {"Content-Type": content_type}

    def _url(self, key: str, expires: int, signature: str) -> str:
        return (
            f"{settings.public_base_url}{settings.api_prefix}/files/{quote(key)}"
            f"?expires={expires}&signature={signature}"
        )

    # ---- chunked uploads -------------------------------------------------
    # Parts are ordinary objects under a staging prefix, which is what lets them reuse the signed
    # PUT route unchanged: a part URL is a normal upload URL that happens to point somewhere the
    # rest of the application never looks. The prefix deliberately sits *outside* the project
    # tree, so a part can never satisfy the "does this key belong to that session" check that
    # registration applies to a finished object.

    #: Everything about an in-flight upload lives under here, keyed by upload id.
    STAGING_PREFIX = "staging"

    def _staging_prefix(self, upload_id: str) -> str:
        if not _UPLOAD_ID.fullmatch(upload_id):
            raise ValidationError("That upload id was not issued by this server")
        return f"{self.STAGING_PREFIX}/{upload_id}"

    def part_key(self, upload_id: str, part_number: int) -> str:
        """Where one part is stored.

        Zero-padded because the parts are listed by name and an unpadded ``10`` sorts before
        ``2`` — an ordering mistake that would assemble the object out of order and leave a file
        that decodes to something almost right.
        """
        if not 1 <= part_number <= 10_000:
            raise ValidationError(f"Part number {part_number} is out of range")
        return f"{self._staging_prefix(upload_id)}/{part_number:05d}.part"

    def begin_multipart(self, key: str, content_type: str) -> str:
        validate_key(key)
        upload_id = uuid.uuid4().hex
        directory = self._resolve(self._staging_prefix(upload_id))
        directory.mkdir(parents=True, exist_ok=True)
        # The manifest binds the upload id to one destination key. Without it a client holding a
        # valid id could complete it into a different key, and both halves would look legitimate
        # on their own.
        (directory / _MANIFEST).write_text(
            json.dumps({"key": key, "content_type": content_type, "started": int(time.time())}),
            encoding="utf-8",
        )
        return upload_id

    def _manifest(self, upload_id: str) -> dict[str, object]:
        path = self._resolve(self._staging_prefix(upload_id)) / _MANIFEST
        if not path.is_file():
            raise NotFoundError("That upload has expired or was never started")
        loaded: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    def _checked_destination(self, key: str, upload_id: str) -> None:
        if self._manifest(upload_id).get("key") != key:
            raise ValidationError("That upload was not started for this object")

    def signed_part_url(
        self, key: str, upload_id: str, part_number: int, ttl_seconds: int | None = None
    ) -> tuple[str, dict[str, str]]:
        self._checked_destination(key, upload_id)
        return self.signed_upload_url(
            self.part_key(upload_id, part_number), "application/octet-stream", ttl_seconds
        )

    def list_multipart_parts(self, key: str, upload_id: str) -> list[ReceivedPart]:
        self._checked_destination(key, upload_id)
        directory = self._resolve(self._staging_prefix(upload_id))
        parts: list[ReceivedPart] = []
        for path in sorted(directory.glob("*.part")):
            try:
                number = int(path.stem)
            except ValueError:
                # Not one of ours. Skipping rather than failing keeps a stray file from making
                # an otherwise complete upload unfinishable.
                continue
            parts.append(ReceivedPart(part_number=number, size_bytes=path.stat().st_size))
        parts.sort(key=lambda part: part.part_number)
        return parts

    def complete_multipart(self, key: str, upload_id: str, content_type: str) -> StoredObject:
        self._checked_destination(key, upload_id)
        directory = self._resolve(self._staging_prefix(upload_id))
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        # Assembled into a temporary file and renamed, like every other write here: until the
        # rename the key does not exist, so an assembly that fails half way cannot be registered
        # as a complete upload.
        partial = target.with_suffix(target.suffix + ".assembling")
        written = 0
        try:
            with partial.open("wb") as out:
                for part in self.list_multipart_parts(key, upload_id):
                    source = directory / f"{part.part_number:05d}.part"
                    with source.open("rb") as handle:
                        shutil.copyfileobj(handle, out, length=1024 * 1024)
                    written += part.size_bytes
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

        self.abort_multipart(key, upload_id)
        return StoredObject(key=key, size_bytes=written, content_type=content_type)

    def abort_multipart(self, key: str, upload_id: str) -> None:
        # No destination check and no error when it is already gone: this runs on the failure
        # path, where the manifest may be exactly what is missing, and a cleanup that can itself
        # fail is a cleanup that leaks.
        with contextlib.suppress(Exception):
            shutil.rmtree(self._resolve(self._staging_prefix(upload_id)), ignore_errors=True)

    def health(self) -> dict[str, Any]:
        """Is the storage root there and writable?

        A read alone would pass on a filesystem that has gone read-only or filled up, which are
        the two ways local storage actually fails — so this writes, and removes what it wrote.
        The probe file lives beside the data rather than in `/tmp`, because a `/tmp` that works
        says nothing about the volume the captures are on.
        """
        probe = self.root / ".health"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe.write_bytes(b"ok")
            probe.unlink(missing_ok=True)
        except Exception as exc:
            return {"backend": self.name, "ok": False, "error": str(exc), "root": str(self.root)}
        return {"backend": self.name, "ok": True, "root": str(self.root)}

    def purge_stale_uploads(self, max_age_seconds: int = 24 * 3600) -> int:
        """Delete parts from uploads nobody finished, and say how many were dropped.

        Object stores solve this with a lifecycle rule; a filesystem has nothing equivalent, so
        an abandoned upload would otherwise hold its bytes for ever. Age is taken from the
        manifest rather than the directory mtime, because writing a part touches the directory
        and would keep a stalled upload looking fresh indefinitely.
        """
        root = self.root / self.STAGING_PREFIX
        if not root.is_dir():
            return 0
        cutoff = time.time() - max_age_seconds
        dropped = 0
        for directory in root.iterdir():
            if not directory.is_dir():
                continue
            manifest = directory / _MANIFEST
            try:
                started = float(json.loads(manifest.read_text(encoding="utf-8"))["started"])
            except Exception:
                # Unreadable manifest, or none at all: fall back to the directory's own age so a
                # corrupted upload is still eventually collected.
                fallback = manifest if manifest.exists() else directory
                started = fallback.stat().st_mtime
            if started < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
                dropped += 1
        return dropped
