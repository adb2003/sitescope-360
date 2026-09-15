"""Signed file transfer for the local storage backend (§24, §44).

Both routes here are deliberately **unauthenticated but signed**: a browser ``<img>`` tag and a
WebGL texture loader cannot attach an Authorization header, and neither can a client PUTting
bytes to a URL it was handed. So the URL carries an HMAC instead, giving capability-based access
with a short lifetime and without ever exposing or accepting a filesystem path.

The signature covers the HTTP verb as well as the key, which is what keeps the two apart: a read
URL -- and there are hundreds of them in flight, in logs, in referrers, in caches -- must not
double as permission to overwrite the object it points at.

With ``STORAGE_BACKEND=s3`` neither route is used. The provider issues native presigned URLs for
both directions and the bytes never transit the API.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.errors import (
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
)
from app.schemas.common import Message
from app.storage import get_storage
from app.storage.base import validate_key
from app.storage.local import LocalStorage

router = APIRouter(tags=["files"])

#: Signed URLs are immutable for their lifetime, so they are safe to cache hard.
CACHE_CONTROL = "private, max-age=3600, immutable"

_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    # Every frame and derivative this pipeline generates. `.jpg` stays because captures processed
    # before the switch are still stored and still served.
    ".webp": "image/webp",
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


@router.put("/files/{key:path}", status_code=status.HTTP_201_CREATED, response_model=Message)
async def receive_file(
    key: str,
    request: Request,
    expires: Annotated[int, Query(description="Unix expiry embedded in the signature")],
    signature: Annotated[str, Query(description="HMAC signature, scoped to PUT")],
) -> Message:
    """Accept bytes for one server-chosen key — the local stand-in for a presigned S3 PUT.

    Unauthenticated but signed, like the GET above, because the whole point of a direct upload is
    that the bytes do not pass through a request the client had to authenticate first. The
    signature covers the verb as well as the key, so a read link cannot be turned into an
    overwrite by changing the method.

    The cap is enforced *while* streaming rather than from `Content-Length`, which a client
    controls and can simply understate. An upload that exceeds it leaves nothing behind.
    """
    storage = get_storage()
    if not isinstance(storage, LocalStorage):
        raise NotFoundError("Direct file upload is only available with the local storage backend")

    validate_key(key)
    if not storage.verify(key, expires, signature, method="PUT"):
        raise PermissionDeniedError("This upload link is invalid or has expired")

    path = storage.path_for_write(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    written = 0
    limit = settings.max_upload_bytes
    try:
        with partial.open("wb") as handle:
            async for chunk in request.stream():
                written += len(chunk)
                if written > limit:
                    raise PayloadTooLargeError(
                        f"Upload exceeds the {limit} byte limit", limit_bytes=limit
                    )
                handle.write(chunk)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    # Rename last: until this point the key does not exist, so a failed or abandoned upload can
    # never be registered as a complete file.
    partial.replace(path)
    return Message(message=f"Stored {written} bytes")
