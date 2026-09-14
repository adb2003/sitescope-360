"""The CDN rewrite that sits between a storage provider and every client.

The rewrite exists to move panorama bytes off the origin, and it is applied to URLs that carry
their own authorisation in the query string. So the property under test is narrow and absolute:
*the path and query must survive untouched*. A rewrite that drops the signature, re-encodes it,
or doubles a slash yields a 403 or a cache miss -- and only once a CDN is actually configured,
which is the one environment nobody exercises before shipping.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.config import Settings, settings
from app.services.assets import to_cdn

#: A local-storage URL: the HMAC covers key and expiry, never the host.
LOCAL_URL = (
    "http://localhost:8000/api/files/projects/p1/floors/f1/sessions/s1/frames/abc.jpg"
    "?expires=1789000000&signature=6f1c2b"
)
#: An S3 SigV4 presigned URL, trimmed to the parameters that matter here.
S3_URL = (
    "https://bucket.s3.eu-west-1.amazonaws.com/projects/p1/frames/abc.jpg"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=3600&X-Amz-Signature=deadbeef"
)


def _validated(value: str | None) -> str | None:
    """Run a candidate through the real field validator rather than a copy of its rules."""
    return Settings(cdn_base_url=value).cdn_base_url


@pytest.fixture
def cdn(monkeypatch: pytest.MonkeyPatch):
    """Configure ``CDN_BASE_URL`` for one test, via the same validation production uses."""

    def _set(value: str | None) -> None:
        monkeypatch.setattr(settings, "cdn_base_url", _validated(value))

    return _set


def test_without_a_cdn_the_url_is_returned_exactly_as_the_provider_built_it(cdn) -> None:
    # The default path is the one running everywhere today; it must not be touched at all.
    cdn(None)
    assert to_cdn(LOCAL_URL) == LOCAL_URL
    assert to_cdn(S3_URL) == S3_URL


@pytest.mark.parametrize("url", [LOCAL_URL, S3_URL], ids=["local", "s3"])
def test_the_signature_and_path_survive_the_rewrite(cdn, url: str) -> None:
    """Only the origin changes. Everything the server will check stays byte-identical."""
    cdn("https://cdn.example.com")
    rewritten = urlsplit(to_cdn(url))
    original = urlsplit(url)

    assert (rewritten.scheme, rewritten.netloc) == ("https", "cdn.example.com")
    assert rewritten.path == original.path
    assert rewritten.query == original.query
    # Parsed as well as compared: a reordered query string is still a valid signature for local
    # storage and a broken one for S3, so the string comparison alone would under-specify this.
    assert parse_qs(rewritten.query) == parse_qs(original.query)


def test_an_http_origin_is_served_over_the_https_cdn(cdn) -> None:
    cdn("https://cdn.example.com")
    assert to_cdn(LOCAL_URL).startswith("https://cdn.example.com/api/files/")


def test_a_base_path_is_prefixed_rather_than_replacing_the_object_path(cdn) -> None:
    """One distribution often serves several apps under a prefix."""
    cdn("https://cdn.example.com/sitescope")
    assert urlsplit(to_cdn(S3_URL)).path == "/sitescope/projects/p1/frames/abc.jpg"


def test_a_trailing_slash_on_the_base_does_not_double_the_separator(cdn) -> None:
    """`//` is a distinct cache key at most edges and a 404 at some origins."""
    cdn("https://cdn.example.com/")
    assert to_cdn(LOCAL_URL) == (
        "https://cdn.example.com/api/files/projects/p1/floors/f1/sessions/s1/frames/abc.jpg"
        "?expires=1789000000&signature=6f1c2b"
    )


def test_an_empty_setting_means_unset_not_an_empty_base(cdn) -> None:
    """`CDN_BASE_URL=` left blank in a .env file must not produce a host-relative URL.

    A browser resolves `/api/files/...` against the page origin and it appears to work; the
    mobile app has no origin to resolve against and fails on every image.
    """
    cdn("   ")
    assert to_cdn(S3_URL) == S3_URL


@pytest.mark.parametrize("value", ["cdn.example.com", "//cdn.example.com", "ftp://cdn.example"])
def test_a_base_without_a_usable_scheme_is_refused_at_start_up(value: str) -> None:
    # ValidationError subclasses ValueError; the point is that boot fails, not first render.
    with pytest.raises(ValueError, match="http"):
        _validated(value)
