"""A ceiling on request bodies, scoped per route (§44).

The gateway allowed 2 GB on *every* endpoint because one of them uploads video. The route that
made it matter is `/auth/login`: it needs no credentials, and FastAPI parses the request body to
build the endpoint's arguments **before** the function runs — so the sign-in rate limiter, added
for exactly this class of problem, never got a say about a huge body.

Two halves have to hold together, and the pairing is the point. Uploads must still accept
gigabytes, or this breaks the feature the project spent the most effort on; everything else must
not, or it protects nothing. A test for either alone would pass with the other broken.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.body_limit import BodySizeLimitMiddleware, upload_patterns
from app.core.config import settings
from tests.conftest import auth_headers


def _limiter() -> BodySizeLimitMiddleware:
    return BodySizeLimitMiddleware(
        app=None,
        default_limit=settings.max_json_body_bytes,
        upload_limit=settings.max_upload_bytes,
        api_prefix=settings.api_prefix,
    )


# ---- which routes get which ceiling ----------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/api/files/projects/p/floors/f/floorplans/x.png",
        "/api/files/staging/0123456789abcdef0123456789abcdef/00001.part",
        "/api/captures/CAP-20260101-000000-AAAA/upload",
        "/api/floors/abc123/floorplan",
    ],
)
def test_the_upload_routes_keep_the_large_ceiling(path: str) -> None:
    assert _limiter().limit_for(path) == settings.max_upload_bytes


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/login",
        "/api/auth/refresh",
        "/api/projects",
        "/api/captures",
        # Near misses, which are the ones a pattern gets wrong: registering a *finished* upload
        # is a small JSON body, and so is asking for a URL to upload to.
        "/api/captures/CAP-20260101-000000-AAAA/files",
        "/api/captures/CAP-20260101-000000-AAAA/upload-url",
        "/api/captures/CAP-20260101-000000-AAAA/multipart",
        "/api/floors/abc123/floorplans",
        "/api/floors/abc123/locations",
    ],
)
def test_everything_else_gets_the_small_ceiling(path: str) -> None:
    assert _limiter().limit_for(path) == settings.max_json_body_bytes


def test_the_patterns_follow_the_configured_api_prefix() -> None:
    """A deployment that mounts the API elsewhere must not silently lose its upload routes.

    Hard-coding `/api` would give every upload the 1 MiB ceiling the moment the prefix changed,
    and the failure would appear as uploads breaking rather than as a limit misapplied.
    """
    patterns = upload_patterns("/v2")

    assert any(pattern.match("/v2/captures/CAP-1/upload") for pattern in patterns)
    assert not any(pattern.match("/api/captures/CAP-1/upload") for pattern in patterns)


# ---- the middleware ---------------------------------------------------------------------------
def test_a_large_body_to_login_is_refused_before_it_is_parsed(client: TestClient) -> None:
    """The case this exists for.

    `Content-Length` is declared honestly here, as any real client would. The point is the
    ordering: this has to be answered before FastAPI builds `LoginRequest`, because that is what
    reads the body into memory — and before the rate limiter, which only runs inside the route.
    """
    oversized = settings.max_json_body_bytes + 1

    response = client.post(
        "/api/auth/login",
        content=b"x" * 16,
        headers={"Content-Type": "application/json", "Content-Length": str(oversized)},
    )

    assert response.status_code == 413, response.text
    assert response.json()["code"] == "payload_too_large"
    assert response.json()["details"]["limit_bytes"] == settings.max_json_body_bytes


def test_an_ordinary_json_request_is_untouched(client: TestClient, admin) -> None:
    # The limit must be invisible in normal use, or it is just a new way for the product to fail.
    headers = auth_headers(client, admin.email)

    created = client.post(
        "/api/projects", json={"name": "Body limit", "code": "BODY-1"}, headers=headers
    )

    assert created.status_code == 201, created.text


def test_a_body_at_the_limit_is_allowed(client: TestClient) -> None:
    """The boundary falls on one side deliberately: at the limit is within it."""
    response = client.post(
        "/api/auth/login",
        content=b"x" * 16,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(settings.max_json_body_bytes),
        },
    )

    # Rejected for being nonsense JSON rather than for its size, which is the distinction.
    assert response.status_code != 413


def test_an_upload_route_still_accepts_a_body_far_over_the_json_limit(
    client: TestClient, admin, panorama_factory
) -> None:
    """The half that would break the product if the scoping were wrong.

    A real upload, larger than the JSON ceiling, through the real route — a pattern that matched
    too broadly would turn every capture upload into a 413.
    """
    headers = auth_headers(client, admin.email)
    project = client.post(
        "/api/projects", json={"name": "Body limit", "code": "BODY-2"}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "A"}, headers=headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "L1", "level": 1, "area_sqft": 100.0},
        headers=headers,
    ).json()
    session = client.post(
        "/api/captures",
        json={"floor_id": floor["id"], "client_session_id": "CAP-20260101-000000-BODY"},
        headers=headers,
    ).json()

    payload = panorama_factory() + b"\0" * settings.max_json_body_bytes
    assert len(payload) > settings.max_json_body_bytes

    uploaded = client.post(
        f"/api/captures/{session['id']}/upload",
        files={"files": ("walk.jpg", payload, "image/jpeg")},
        headers=headers,
    )

    assert uploaded.status_code == 201, uploaded.text


def test_a_request_with_no_declared_length_is_passed_through(client: TestClient) -> None:
    """Only `Content-Length` is judged here, and the docstring says so rather than implying more.

    A chunked request carries no length; nginx bounds those across the accumulated body, and this
    deliberately does not wrap `receive` to count bytes in front of a 2 GB upload. Asserting the
    documented behaviour so the limitation stays visible instead of being assumed away.
    """
    response = client.post(
        "/api/auth/login",
        content=b'{"email":"a@b.com","password":"x"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code != 413


def test_a_malformed_content_length_is_not_guessed_at(client: TestClient) -> None:
    """Deferring to the server's own parser rather than inventing a size to compare."""
    limiter = _limiter()

    # The middleware's own view: an unparseable length yields no decision.
    scope = {
        "type": "http",
        "path": "/api/auth/login",
        "headers": [(b"content-length", b"not-a-number")],
    }
    from app.api.body_limit import _declared_length

    assert _declared_length(scope) is None
    assert limiter.limit_for("/api/auth/login") == settings.max_json_body_bytes


def test_non_http_traffic_is_left_alone() -> None:
    """WebSocket scopes have no body to limit, and must not be answered with a 413."""
    seen: list[str] = []

    async def inner(scope: dict, receive: object, send: object) -> None:
        seen.append(scope["type"])

    middleware = BodySizeLimitMiddleware(
        app=inner,
        default_limit=settings.max_json_body_bytes,
        upload_limit=settings.max_upload_bytes,
        api_prefix=settings.api_prefix,
    )

    import asyncio

    asyncio.run(middleware({"type": "websocket", "path": "/api/events"}, None, None))  # type: ignore[arg-type]

    assert seen == ["websocket"]
