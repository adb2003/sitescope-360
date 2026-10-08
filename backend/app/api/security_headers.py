"""Response headers that tell a browser what this API is not (§44).

The gateway already sets a good set of these — `nginx.conf` has had `nosniff`, a referrer policy,
`X-Frame-Options` and a full CSP from the start. Two things were still wrong, and they are the
same thing seen from both ends.

**The application set none of its own.** Every header above lives in a config file that one
deployment shape happens to mount. Run the API behind a cloud load balancer, a Kubernetes
ingress, or plain `uvicorn` — all of which this codebase supports, none of which read
`infrastructure/nginx/nginx.conf` — and nothing is set at all. That is the same argument
`body_limit.py` makes about `client_max_body_size`, and it is no weaker here.

**The gateway's CSP broke `/docs`.** `script-src 'self'` is correct for the SPA and wrong for the
two pages this API serves itself: FastAPI's Swagger and ReDoc HTML load their viewer from
`cdn.jsdelivr.net` and initialise it from an inline `<script>`, so the production stack served a
documented, advertised page that rendered blank. Measured, not suspected — the HTML is fetched
and its origins are compared against this policy by `test_security_headers.py`, so this cannot
drift back without a test going red.

So headers are owned by whoever generates the response. The application sets them for everything
it serves; the gateway now sets them in the one location it answers for itself, the SPA.
Duplication is not harmless and that is why the split is worth the churn: two `Content-Security-
Policy` headers are *both* enforced and the browser applies the intersection, so adding a second
one at the proxy would have re-broken the docs page no matter what the application said.

Two headers are deliberately absent:

* **`Cross-Origin-Resource-Policy`.** Signed asset URLs are loaded cross-origin by design —
  `CDN_BASE_URL` exists to make that the normal case — and `same-site` would break exactly the
  WebGL texture loads the viewer is built on.
* **`Permissions-Policy` on data responses.** It governs a document's use of device APIs, and a
  JSON body is not a document. It is set on the docs pages, where it means something.

HSTS is off unless configured, because switching it on is a claim about a deployment rather than
a preference: sent from a host that is not entirely on HTTPS, it locks a browser out of that host
for the lifetime of the `max-age`, and no response from here can undo it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

#: Applied to every response: a stored file must not be sniffed into something executable, and a
#: URL carrying a share token must not travel to another origin in a `Referer`.
BASE_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "strict-origin-when-cross-origin"),
    ("x-frame-options", "DENY"),
)

#: The only CSP directive that means anything for a response which is not a document.
#:
#: `frame-ancestors` is honoured for anything that *can* be framed and inert for everything else,
#: which is why the data policy is this and not `default-src 'none'`: a PDF served inline by
#: `/api/files/...` becomes a document when it is opened directly, and a blanket `'none'` would
#: be betting on how each browser's built-in viewer is implemented. Nothing in this product
#: frames an API response — there is not an `<iframe>` or `<embed>` in the web or mobile client.
DATA_CSP = "frame-ancestors 'none'"

#: Third-party origins the FastAPI documentation pages fetch from.
#:
#: Named individually rather than waved through, because this is the one place in the product
#: where a page loads code it did not build. A test fetches both pages and asserts every external
#: origin in their HTML appears here, so a FastAPI upgrade that moves a CDN fails rather than
#: silently serving a blank page.
#:
#: That test is necessary and not sufficient, which is worth stating: `cdn.redoc.ly` below appears
#: nowhere in the HTML. ReDoc's bundle asks for its own logo at runtime, so the only way to find
#: it was to load the page in a browser and read the console — which is how this list was
#: checked, and why a reader should not trust it further than that.
DOCS_ORIGINS: tuple[str, ...] = (
    "https://cdn.jsdelivr.net",
    "https://fonts.googleapis.com",
    "https://fonts.gstatic.com",
    "https://fastapi.tiangolo.com",
    "https://cdn.redoc.ly",
)

#: The policy for the two HTML pages this API serves.
#:
#: `'unsafe-inline'` in `script-src` is load-bearing and not an oversight: FastAPI's docs HTML
#: calls `SwaggerUIBundle(...)` from an inline `<script>`, so without it the page loads its
#: bundle and never starts it. Scoped to these two paths, which reflect no user input — the
#: OpenAPI schema they render comes from this repository — so the allowance cannot weaken any
#: route that handles somebody's data. The test asserts the dependency in both directions: if a
#: future FastAPI stops needing inline script, the test fails and this comes out.
DOCUMENT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    # `cdn.redoc.ly` is ReDoc's own logo, requested by the bundle rather than named in the HTML.
    # A decorative image, and it is allowed rather than left blocked on purpose: a policy that
    # logs a violation on every load of a page that works is how people learn to ignore them.
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    # ReDoc parses the schema in a worker it creates from a blob URL.
    "worker-src 'self' blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)

DOCUMENT_PERMISSIONS = "geolocation=(), camera=(), microphone=()"


def headers_for(*, document: bool) -> tuple[tuple[str, str], ...]:
    """The header set for one response, as lowercase name/value pairs."""
    if document:
        return (
            *BASE_HEADERS,
            ("content-security-policy", DOCUMENT_CSP),
            ("permissions-policy", DOCUMENT_PERMISSIONS),
        )
    return (*BASE_HEADERS, ("content-security-policy", DATA_CSP))


class SecurityHeadersMiddleware:
    """Add the headers above to every HTTP response that does not already carry them.

    Raw ASGI rather than `BaseHTTPMiddleware`, for the same reason as the body limit: this has to
    work for a `FileResponse` streaming two gigabytes as readily as for a JSON body, and
    `BaseHTTPMiddleware` buffers.

    *Does not already carry them* is the whole contract. `/api/files/{key}` sets its own
    `nosniff` next to the `Content-Disposition` and allow-listed media type that make it
    meaningful, and a route that has thought about its own framing or caching must win over a
    blanket default — otherwise this middleware becomes a thing people work around.
    """

    def __init__(
        self,
        app: Any,
        *,
        document_paths: frozenset[str],
        hsts_max_age: int = 0,
    ) -> None:
        self.app = app
        self.document_paths = document_paths
        self.hsts_max_age = hsts_max_age

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # A WebSocket handshake has no response headers to set, and the lifespan scope has no
        # response at all.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        document = scope.get("path", "") in self.document_paths
        # Only on HTTPS. `scope["scheme"]` is rewritten from `X-Forwarded-Proto` by uvicorn's
        # `--proxy-headers`, so a TLS-terminating proxy is covered; a plain HTTP request gets no
        # HSTS, because a browser that cannot yet reach this host over TLS must not be told to
        # stop trying anything else.
        secure = scope.get("scheme") == "https"
        extra = headers_for(document=document)
        if self.hsts_max_age > 0 and secure:
            extra = (
                *extra,
                (
                    "strict-transport-security",
                    f"max-age={self.hsts_max_age}; includeSubDomains",
                ),
            )

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = _merge(message.get("headers", []), extra)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _merge(
    existing: list[tuple[bytes, bytes]], extra: tuple[tuple[str, str], ...]
) -> list[tuple[bytes, bytes]]:
    """Append the headers the response does not already name, leaving its own alone."""
    present = {name.lower() for name, _value in existing}
    return [
        *existing,
        *(
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in extra
            if name.encode("latin-1") not in present
        ),
    ]
