"""Signed file serving for the local storage backend (§24, §44).

Deliberately **unauthenticated but signed**: a browser ``<img>`` tag and a WebGL texture loader
cannot attach an Authorization header, so asset URLs carry an HMAC over ``key:expires`` instead.
That gives capability-based access with a short lifetime, without ever exposing a filesystem path
or accepting a client-supplied path.

With ``STORAGE_BACKEND=s3`` this route is unused — the provider issues native presigned URLs and
the bytes never transit the API.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi.responses import FileResponse

from app.core.errors import NotFoundError, PermissionDeniedError
from app.storage import get_storage
from app.storage.base import validate_key
from app.storage.local import LocalStorage

router = APIRouter(tags=["files"])

#: Signed URLs are immutable for their lifetime, so they are safe to cache hard.
CACHE_CONTROL = "private, max-age=3600, immutable"

_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".pdf": "application/pdf",
    ".npz": "application/octet-stream",
    ".csv": "text/csv",
    ".json": "application/json",
}


@router.get("/files/{key:path}")
def serve_file(
    key: str,
    expires: Annotated[int, Query(description="Unix expiry embedded in the signature")],
    signature: Annotated[str, Query(description="HMAC signature")],
) -> Response:
    storage = get_storage()
    if not isinstance(storage, LocalStorage):
        raise NotFoundError("Direct file serving is only available with the local storage backend")

    validate_key(key)
    if not storage.verify(key, expires, signature):
        raise PermissionDeniedError("This file link is invalid or has expired")

    path = storage.local_path(key)
    if path is None:
        raise NotFoundError("File not found")

    from pathlib import PurePosixPath

    suffix = PurePosixPath(key).suffix.lower()
    media_type = _CONTENT_TYPES.get(suffix, "application/octet-stream")

    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": CACHE_CONTROL,
            # Never let a browser sniff a stored file into something executable.
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{PurePosixPath(key).name}"',
        },
    )
