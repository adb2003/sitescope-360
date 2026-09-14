"""Local filesystem storage — the MVP default (§24).

Client-facing URLs are HMAC-signed and time-limited and are served by the API, so a filesystem
path is never exposed and an object cannot be fetched by guessing a key (§44).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import shutil
import time
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from app.core.config import settings
from app.core.errors import NotFoundError
from app.storage.base import StoredObject, validate_key


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
