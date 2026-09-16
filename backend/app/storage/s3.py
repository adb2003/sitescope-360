"""S3-compatible object storage (§24).

Production-ready alternative to :class:`~app.storage.local.LocalStorage`. ``boto3`` is imported
lazily so the MVP install stays lean — install it only when ``STORAGE_BACKEND=s3``:

    uv pip install boto3
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any, BinaryIO

from app.core.config import settings
from app.core.errors import NotFoundError, SiteScopeError
from app.storage.base import StoredObject, validate_key
from app.storage.multipart import ReceivedPart

if TYPE_CHECKING:  # pragma: no cover
    pass


class S3Storage:
    name = "s3"

    def __init__(
        self,
        bucket: str | None = None,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover
            raise SiteScopeError(
                "S3 storage requires boto3. Install it with: uv pip install boto3"
            ) from exc

        self.bucket = bucket or settings.s3_bucket
        if not self.bucket:
            raise SiteScopeError("S3_BUCKET must be set when STORAGE_BACKEND=s3")

        self._client: Any = boto3.client(
            "s3",
            region_name=region or settings.s3_region,
            endpoint_url=endpoint_url or settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
        )

    def put(self, key: str, data: BinaryIO, content_type: str) -> StoredObject:
        validate_key(key)
        self._client.upload_fileobj(data, self.bucket, key, ExtraArgs={"ContentType": content_type})
        return StoredObject(key=key, size_bytes=self.size(key), content_type=content_type)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> StoredObject:
        validate_key(key)
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return StoredObject(key=key, size_bytes=len(data), content_type=content_type)

    def open(self, key: str) -> BinaryIO:
        return io.BytesIO(self.read_bytes(key))

    def read_bytes(self, key: str) -> bytes:
        validate_key(key)
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise NotFoundError(f"Object not found: {key}") from exc
        body: bytes = response["Body"].read()
        return body

    def read_range(self, key: str, length: int, start: int = 0) -> bytes:
        validate_key(key)
        # HTTP ranges are inclusive at both ends, so the last byte is start + length - 1. Off by
        # one here would silently return one byte too many or too few to the magic-byte sniff.
        end = start + max(1, length) - 1
        try:
            response = self._client.get_object(
                Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}"
            )
        except Exception as exc:
            raise NotFoundError(f"Object not found: {key}") from exc
        body: bytes = response["Body"].read()
        return body

    def exists(self, key: str) -> bool:
        validate_key(key)
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def size(self, key: str) -> int:
        validate_key(key)
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise NotFoundError(f"Object not found: {key}") from exc
        return int(head["ContentLength"])

    def local_path(self, key: str) -> str | None:
        # Objects live remotely; callers needing a path must stage a temporary copy.
        return None

    def delete(self, key: str) -> None:
        validate_key(key)
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def delete_prefix(self, prefix: str) -> int:
        validate_key(prefix)
        paginator = self._client.get_paginator("list_objects_v2")
        deleted = 0
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if objects:
                self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects})
                deleted += len(objects)
        return deleted

    def signed_upload_url(
        self, key: str, content_type: str, ttl_seconds: int | None = None
    ) -> tuple[str, dict[str, str]]:
        """A presigned PUT, plus the headers the client is then *obliged* to send.

        ``ContentType`` is part of the signature, so a client that omits the header -- or sends a
        different one -- gets a 403 from S3 rather than an object with the wrong type. Returning
        the headers alongside the URL is what keeps that from being a trap: the caller does not
        have to know which parameters were signed.

        S3 cannot enforce a maximum size on a presigned PUT (only a POST policy can), so the cap
        is applied when the upload is registered, and an oversized object is deleted there.
        """
        validate_key(key)
        url: str = self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=int(ttl_seconds or settings.storage_url_ttl_seconds),
        )
        return url, {"Content-Type": content_type}

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str:
        validate_key(key)
        url: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=int(ttl_seconds or settings.storage_url_ttl_seconds),
        )
        return url

    # ---- chunked uploads -------------------------------------------------
    def begin_multipart(self, key: str, content_type: str) -> str:
        validate_key(key)
        response = self._client.create_multipart_upload(
            Bucket=self.bucket, Key=key, ContentType=content_type
        )
        upload_id: str = response["UploadId"]
        return upload_id

    def signed_part_url(
        self, key: str, upload_id: str, part_number: int, ttl_seconds: int | None = None
    ) -> tuple[str, dict[str, str]]:
        """A presigned ``UploadPart``.

        No headers come back with it, and that is the point: a part has no content type of its
        own — the object's was fixed when the upload was created — so nothing beyond the key,
        the upload id and the part number is signed. A client that adds headers is not fighting
        the signature, which is one fewer way for a long transfer to fail at part 200.
        """
        validate_key(key)
        url: str = self._client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": int(part_number),
            },
            ExpiresIn=int(ttl_seconds or settings.storage_url_ttl_seconds),
        )
        return url, {}

    def _parts(self, key: str, upload_id: str) -> list[dict[str, Any]]:
        """Every part S3 is holding, paginated — `list_parts` returns 1000 at a time."""
        paginator = self._client.get_paginator("list_parts")
        found: list[dict[str, Any]] = []
        for page in paginator.paginate(Bucket=self.bucket, Key=key, UploadId=upload_id):
            found.extend(page.get("Parts", []))
        found.sort(key=lambda part: int(part["PartNumber"]))
        return found

    def list_multipart_parts(self, key: str, upload_id: str) -> list[ReceivedPart]:
        validate_key(key)
        try:
            parts = self._parts(key, upload_id)
        except Exception as exc:
            raise NotFoundError("That upload has expired or was never started") from exc
        return [
            ReceivedPart(part_number=int(part["PartNumber"]), size_bytes=int(part["Size"]))
            for part in parts
        ]

    def complete_multipart(self, key: str, upload_id: str, content_type: str) -> StoredObject:
        validate_key(key)
        parts = self._parts(key, upload_id)
        if not parts:
            raise NotFoundError("No parts were uploaded for that upload id")
        # The ETags are S3's own, read back rather than taken from the client. A client-supplied
        # ETag is a claim about bytes the server has not seen; this is the bytes it is holding.
        self._client.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"ETag": part["ETag"], "PartNumber": int(part["PartNumber"])} for part in parts
                ]
            },
        )
        return StoredObject(key=key, size_bytes=self.size(key), content_type=content_type)

    def abort_multipart(self, key: str, upload_id: str) -> None:
        validate_key(key)
        # Suppressed because this runs on the failure path. S3 keeps charging for the parts of an
        # upload that is neither completed nor aborted, so a bucket lifecycle rule
        # (`AbortIncompleteMultipartUpload`) is the backstop for the cases this cannot reach --
        # a client that simply stops calling.
        try:
            self._client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
        except Exception:
            return
