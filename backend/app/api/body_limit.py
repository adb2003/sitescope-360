"""A ceiling on request bodies, scoped to what each route legitimately needs (§44).

The gateway's `client_max_body_size` is 2 GB because a walkthrough video is, and it was set at
the http level — so *every* endpoint accepted 2 GB, not just the three that upload files. The
sharp edge is the one route that needs no credentials: FastAPI parses and validates a request
body to build the endpoint's arguments **before** the function runs, so a huge JSON body posted
to `/auth/login` is read and parsed before `ratelimit.check_address()` gets a say. The rate limit
added for exactly this class of problem does not cover it.

Worse without the gateway. Neither Starlette nor uvicorn imposes a default body limit, so an API
exposed directly had no ceiling at all.

**Scoped by path, deliberately, and not by content type.** Limiting on `Content-Type` would be
more robust against routes moving, and is the wrong control here: the content type is chosen by
the caller, so anything wanting a large body would simply claim to be an upload. The paths below
are few and stable, and there are tests asserting both halves — that each upload route still
accepts something large, and that ordinary routes do not.

**Only `Content-Length` is checked.** A request that declares no length (chunked transfer) is
bounded by nginx, which enforces its limit across the accumulated body, but not here. Counting
bytes as they stream would mean wrapping `receive` in front of a 2 GB upload, and a bug there
breaks the uploads this project spent real effort making reliable — a poor trade against a case
that no ordinary client produces. Stated rather than left for someone to discover.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def upload_patterns(prefix: str) -> tuple[re.Pattern[str], ...]:
    """The routes that legitimately carry more than a form's worth of data.

    Built from the configured API prefix rather than hard-coded, so a deployment that mounts the
    API somewhere else does not silently lose its upload routes to the small limit.
    """
    escaped = re.escape(prefix.rstrip("/"))
    return (
        # Signed PUT: direct uploads and the individual parts of a chunked one.
        re.compile(rf"^{escaped}/files/"),
        # The proxied upload, which streams the body through the API.
        re.compile(rf"^{escaped}/captures/[^/]+/upload$"),
        # Floor-plan sheets. Bounded far lower by the route itself (64 MiB), but well above the
        # limit an ordinary JSON body gets.
        re.compile(rf"^{escaped}/floors/[^/]+/floorplan$"),
    )


class BodySizeLimitMiddleware:
    """Reject an over-large body before the application reads it.

    Raw ASGI rather than `BaseHTTPMiddleware`: this has to answer *before* the request reaches
    routing and body parsing, which is the entire point — a limit that runs after the JSON has
    been parsed has already paid the cost it exists to prevent.
    """

    def __init__(
        self,
        app: Any,
        *,
        default_limit: int,
        upload_limit: int,
        api_prefix: str,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.upload_limit = upload_limit
        self._uploads = upload_patterns(api_prefix)

    def limit_for(self, path: str) -> int:
        return (
            self.upload_limit
            if any(pattern.match(path) for pattern in self._uploads)
            else self.default_limit
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None:
            limit = self.limit_for(scope.get("path", ""))
            if declared > limit:
                await _refuse(send, limit)
                return

        await self.app(scope, receive, send)


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                # A malformed length is not a size this can judge. The server's own parser will
                # reject the request; guessing a number here would be worse than deferring.
                return None
    return None


async def _refuse(send: Send, limit: int) -> None:
    """Answer with the same `{code, message}` envelope every other refusal uses.

    Written by hand because this runs outside the application's exception handlers — it has to,
    to answer before routing — so `PayloadTooLargeError` would have nobody to catch it.
    """
    body = json.dumps(
        {
            "code": "payload_too_large",
            "message": (
                f"Request body exceeds the {limit} byte limit for this endpoint. "
                "Large files belong on the upload routes."
            ),
            "details": {"limit_bytes": limit},
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
