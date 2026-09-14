"""Turning storage keys into client-facing signed assets (§24, §44).

Every asset URL the API hands out is built here, which is what makes the CDN rewrite below a
single change rather than one per route.
"""

from __future__ import annotations

import posixpath
from urllib.parse import urlsplit, urlunsplit

from app.core.config import settings
from app.schemas.common import SignedAsset
from app.storage import get_storage


def to_cdn(url: str) -> str:
    """Re-point an origin URL at ``CDN_BASE_URL``, or return it unchanged when that is unset.

    Only the scheme, host and any path prefix on the base are replaced. The object path, query
    and fragment are carried through byte for byte, because both providers put their
    authorisation *there* -- ``expires``/``signature`` for local storage, ``X-Amz-*`` for S3.
    Rewriting or re-encoding any of it would turn a working URL into a 403 that only appears
    once a CDN is actually configured, which is the worst moment to find out.
    """
    base = settings.cdn_base_url
    if not base:
        return url
    origin = urlsplit(url)
    prefix = urlsplit(base)
    # `posixpath.join` and not f-string concatenation: the base has already had its trailing
    # slash stripped, so joining by hand would produce `//` on every URL and defeat cache keys
    # that treat the two spellings as distinct objects.
    path = posixpath.join(prefix.path, origin.path.lstrip("/")) if prefix.path else origin.path
    return urlunsplit((prefix.scheme, prefix.netloc, path, origin.query, origin.fragment))


def sign(key: str | None, ttl_seconds: int | None = None) -> SignedAsset | None:
    """Never returns a filesystem path — only a time-limited URL."""
    if not key:
        return None
    ttl = int(ttl_seconds or settings.storage_url_ttl_seconds)
    try:
        url = get_storage().signed_url(key, ttl)
    except Exception:
        return None
    return SignedAsset(key=key, url=to_cdn(url), expires_in=ttl)


def sign_url(key: str | None, ttl_seconds: int | None = None) -> str | None:
    asset = sign(key, ttl_seconds)
    return asset.url if asset else None
