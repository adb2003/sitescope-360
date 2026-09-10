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

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str:
        validate_key(key)
        url: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=int(ttl_seconds or settings.storage_url_ttl_seconds),
        )
        return url
