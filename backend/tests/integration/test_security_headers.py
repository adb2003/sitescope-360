"""Every response says what it is not, and the documentation pages still work (§44).

Two faults, one cause. The gateway config was the only place security headers existed, so an API
run any other way — a cloud load balancer, an ingress, plain uvicorn — sent none. And the policy
written for the SPA was wrong for the two pages the API serves itself: FastAPI's Swagger and
ReDoc HTML load their viewer from `cdn.jsdelivr.net` and start it from an inline `<script>`, both
of which `script-src 'self'` forbids, so the production stack served an advertised documentation
page that rendered blank.

The second fault is the interesting one to test, because a header test that only asserts the
header is present would have passed on the broken policy. So the docs tests compare the policy
against the page it has to permit: every external origin in the HTML must be allowed, and the
inline script the page depends on must be allowed *while it depends on it* — if a future FastAPI
stops needing `'unsafe-inline'`, the test fails and the allowance comes out.

What a test here cannot do is run the page, so a policy can still be wrong in a way nothing below
catches: `cdn.redoc.ly` appears nowhere in ReDoc's HTML — its bundle asks for its own logo at
runtime — and was found by loading both pages in a browser and reading the console. Recorded here
so the next person knows the ceiling of this file.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.api.security_headers import (
    DATA_CSP,
    DOCS_ORIGINS,
    DOCUMENT_CSP,
    SecurityHeadersMiddleware,
)
from app.core.config import settings
from app.main import app
from tests.conftest import auth_headers

EXPECTED_BASE = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
}


# ---- the headers the application had none of ---------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/",  # the root document
        "/health",  # the liveness probe, which touches no middleware of its own
        "/api/system/health",
        "/openapi.json",
        "/api/projects",  # 401: an unauthenticated refusal is still a response
        "/api/does-not-exist",  # 404 from the router
    ],
)
def test_every_response_carries_the_base_headers(client: TestClient, path: str) -> None:
    response = client.get(path)

    for name, value in EXPECTED_BASE.items():
        assert response.headers.get(name) == value, f"{path} → {name}"
    assert response.headers["content-security-policy"] == DATA_CSP


def test_a_validation_error_carries_them(client: TestClient) -> None:
    """422s are written by the application's own handler, inside the middleware stack."""
    response = client.post("/api/auth/login", json={"email": "not-an-email"})

    assert response.status_code == 422
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_body_limit_refusal_carries_them(client: TestClient) -> None:
    """Written by hand in raw ASGI, outside the exception handlers — and still wrapped."""
    oversized = settings.max_json_body_bytes + 1
    response = client.post(
        "/api/auth/login",
        content=b"x" * 16,
        headers={"Content-Type": "application/json", "Content-Length": str(oversized)},
    )

    assert response.status_code == 413
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == DATA_CSP


def test_the_500_page_carries_them() -> None:
    """The one response outside the middleware stack, so it sets the headers itself.

    A handler registered for `Exception` becomes `ServerErrorMiddleware`'s, which wraps
    everything the application adds — so this is the only path where the middleware never sees
    the response, and the only reason `main.py` repeats the header set.
    """

    @app.get("/__boom_headers")
    def _boom() -> dict[str, str]:  # pragma: no cover - raises by design
        raise RuntimeError("boom")

    try:
        with TestClient(app, raise_server_exceptions=False) as raw:
            response = raw.get("/__boom_headers")

        assert response.status_code == 500
        for name, value in EXPECTED_BASE.items():
            assert response.headers.get(name) == value, name
        assert response.headers["content-security-policy"] == DATA_CSP
    finally:
        app.router.routes = [
            route for route in app.router.routes if getattr(route, "path", "") != "/__boom_headers"
        ]
        app.openapi_schema = None


# ---- what the middleware must not do -----------------------------------------
def test_a_route_that_set_its_own_header_keeps_it(client: TestClient, storage_root) -> None:
    """`/api/files/{key}` reasons about its own response; a blanket default must not overwrite it.

    It sets `nosniff` next to an allow-listed media type and a `Content-Disposition`, which is
    the combination that makes it mean something. The middleware adds what is missing and leaves
    the rest alone — otherwise it becomes a thing routes work around.
    """
    from app.services.assets import sign
    from app.storage import get_storage

    key = "projects/headers-demo/floors/f1/sessions/CAP-H/frames/a.jpg"
    get_storage().put_bytes(key, b"\xff\xd8\xff\xd9" * 8, "image/jpeg")
    signed = sign(key)
    assert signed is not None

    response = client.get(signed.url.replace("http://testserver", ""))

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "image/jpeg"
    assert "inline" in response.headers["content-disposition"]
    # And the ones it did not set are now there too.
    assert response.headers["x-frame-options"] == "DENY"


def test_an_existing_value_is_not_duplicated_or_replaced() -> None:
    """The merge, directly: a response naming a header keeps its own value, exactly once."""
    from app.api.security_headers import _merge, headers_for

    merged = _merge(
        [(b"x-content-type-options", b"something-else"), (b"content-type", b"text/plain")],
        headers_for(document=False),
    )
    names = [name.decode() for name, _value in merged]

    assert names.count("x-content-type-options") == 1
    assert dict(merged)[b"x-content-type-options"] == b"something-else"
    assert b"x-frame-options" in dict(merged)


def test_non_http_traffic_is_left_alone() -> None:
    """A WebSocket handshake has no response headers to set, and must pass straight through."""
    import asyncio

    seen: list[str] = []

    async def inner(scope: dict, receive: object, send: object) -> None:
        seen.append(scope["type"])

    middleware = SecurityHeadersMiddleware(
        app=inner, document_paths=frozenset({"/docs"}), hsts_max_age=0
    )
    asyncio.run(middleware({"type": "websocket", "path": "/api/events"}, None, None))  # type: ignore[arg-type]

    assert seen == ["websocket"]


# ---- the policy that broke the documentation ---------------------------------
EXTERNAL_URL = re.compile(r"https?://[^\s\"'<>]+")


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_a_documentation_page_gets_the_document_policy(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == DOCUMENT_CSP
    assert response.headers["permissions-policy"] == "geolocation=(), camera=(), microphone=()"


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_every_origin_the_page_fetches_is_allowed(client: TestClient, path: str) -> None:
    """The regression test for the blank page: compare the policy against the actual HTML.

    A FastAPI upgrade that moves its CDN, or a tightening of the policy that forgets one of
    these, fails here instead of being discovered by someone opening the page.
    """
    html = client.get(path).text
    origins = {
        "/".join(url.split("/")[:3])
        for url in EXTERNAL_URL.findall(html)
        # Only subresources matter. Links the page merely points at are not fetched by it.
        if not url.startswith(("https://github.com", "https://www.", "http://localhost"))
    }

    assert origins, f"{path} fetched nothing external — has FastAPI started self-hosting?"
    unlisted = origins - set(DOCS_ORIGINS)
    assert not unlisted, f"{path} fetches {sorted(unlisted)}, which DOCUMENT_CSP does not allow"
    for origin in origins:
        assert origin in DOCUMENT_CSP, f"{origin} is listed but not in the policy"


def test_inline_script_is_allowed_only_while_a_page_needs_it(client: TestClient) -> None:
    """Asserted in both directions, so the allowance cannot outlive its reason.

    Swagger's HTML calls `SwaggerUIBundle(...)` from an inline `<script>`: without
    `'unsafe-inline'` the page loads its bundle and never starts it. ReDoc's does not — it only
    has an inline `<style>` — so the claim is about the pair, since they share one policy. If a
    future FastAPI ships HTML that needs neither, this fails and the allowance comes out.
    """
    inline = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S")
    needs_it = {path for path in ("/docs", "/redoc") if inline.search(client.get(path).text)}
    allows_inline = "'unsafe-inline'" in DOCUMENT_CSP.split("script-src")[1].split(";")[0]

    assert bool(needs_it) == allows_inline, (
        f"pages with inline script: {sorted(needs_it) or 'none'}, policy allows it: {allows_inline}"
    )
    # The specific dependency, so a change in which page needs it is visible rather than averaged
    # away by the line above.
    assert needs_it == {"/docs"}, sorted(needs_it)


def test_the_api_itself_never_gets_the_document_policy(client: TestClient) -> None:
    """`/openapi.json` is data, and the oauth2 redirect page is not one of the two.

    The redirect page is pure inline script, so handing it the document policy would be the one
    place this API allowed inline script on a page it has no use for.
    """
    for path in ("/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).headers["content-security-policy"] == DATA_CSP, path


# ---- HSTS, which is the one that cannot be taken back ------------------------
def test_hsts_is_not_sent_by_default(client: TestClient) -> None:
    assert settings.hsts_max_age_seconds == 0
    assert "strict-transport-security" not in client.get("/health").headers


def test_hsts_is_sent_only_over_https() -> None:
    """Configured *and* arrived over TLS. A browser that cannot reach this host over HTTPS yet
    must not be told to stop trying anything else."""
    import asyncio

    captured: dict[str, str] = {}

    async def inner(scope: dict, receive: object, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def collect(message: dict) -> None:
        if message["type"] == "http.response.start":
            captured.update({name.decode(): value.decode() for name, value in message["headers"]})

    middleware = SecurityHeadersMiddleware(
        app=inner, document_paths=frozenset(), hsts_max_age=31536000
    )

    asyncio.run(middleware({"type": "http", "path": "/x", "scheme": "http"}, None, collect))  # type: ignore[arg-type]
    assert "strict-transport-security" not in captured

    captured.clear()
    asyncio.run(middleware({"type": "http", "path": "/x", "scheme": "https"}, None, collect))  # type: ignore[arg-type]
    assert captured["strict-transport-security"] == "max-age=31536000; includeSubDomains"


# ---- the share page, which is what prompted all of this ----------------------
def test_the_public_share_payload_refuses_framing(client: TestClient) -> None:
    """The one route that answers with no account at all.

    The page a visitor opens is the SPA's `/s/{token}`, and framing it is the gateway's call —
    `frame-ancestors 'self'` in `location /`. This is the API half: the payload behind it, which
    needs no credential beyond the token in its URL, and which nothing in this product frames.
    """
    response = client.get("/api/shared/" + "z" * 40)

    # 404 whatever the reason, as that route documents — the headers are the point here.
    assert response.status_code == 404
    assert response.headers["content-security-policy"] == DATA_CSP
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_an_authenticated_json_response_carries_them(client: TestClient, admin) -> None:
    """The ordinary case, which is most of the API."""
    response = client.get("/api/projects", headers=auth_headers(client, "admin@test.example.com"))

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == DATA_CSP
