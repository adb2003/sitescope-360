"""Turning storage keys into client-facing signed assets (§24, §44)."""

from __future__ import annotations

from app.core.config import settings
from app.schemas.common import SignedAsset
from app.storage import get_storage


def sign(key: str | None, ttl_seconds: int | None = None) -> SignedAsset | None:
    """Never returns a filesystem path — only a time-limited URL."""
    if not key:
        return None
    ttl = int(ttl_seconds or settings.storage_url_ttl_seconds)
    try:
        url = get_storage().signed_url(key, ttl)
    except Exception:
        return None
    return SignedAsset(key=key, url=url, expires_in=ttl)


def sign_url(key: str | None, ttl_seconds: int | None = None) -> str | None:
    asset = sign(key, ttl_seconds)
    return asset.url if asset else None
