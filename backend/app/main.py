"""SiteScope 360 — FastAPI application (§25, §26).

An independent construction 360° site monitoring platform. No OpenSpace (or other proprietary
construction-monitoring vendor) API, SDK, application, account or code is used anywhere in this
service; see docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.body_limit import BodySizeLimitMiddleware
from app.api.routes import (
    admin,
    auth,
    cameras,
    captures,
    events,
    files,
    floorplans,
    monitoring,
    projects,
    reports,
    shares,
    sync,
)

# `annotations` is aliased: an unaliased import would rebind the name bound by
# `from __future__ import annotations` above, which works at runtime but is a genuine hazard
# and defeats static checking.
from app.api.routes import annotations as annotation_routes
from app.api.security_headers import SecurityHeadersMiddleware, headers_for
from app.core.config import settings
from app.core.errors import RateLimitedError, SiteScopeError
from app.core.logging import configure_logging, correlation_id, get_logger, log_event

logger = get_logger("sitescope.api")

DESCRIPTION = """
An independent construction 360° site capture and monitoring platform.

**Independence:** this API does not use, wrap, proxy or depend on OpenSpace or any other
proprietary construction-monitoring platform. Capture processing, panorama viewing, floor-plan
mapping, coverage and reporting are implemented in this repository on open-source components.

**Honesty about analysis** (§46): every positional or derived value carries a `provenance` of
`measured`, `estimated`, `ai_predicted` or `manual`. Automatic mapping reports a confidence and
refuses to publish a path it does not believe. Unimplemented capabilities are reported as
unavailable by `GET /api/system/capabilities` rather than returning plausible numbers.
"""

TAGS_METADATA = [
    {"name": "auth", "description": "Sign in, token refresh, current session."},
    {"name": "users", "description": "User administration (admin only)."},
    {"name": "projects", "description": "Projects, buildings, floors, zones, schedules."},
    {"name": "floor plans", "description": "Floor-plan upload, calibration, capture locations."},
    {"name": "captures", "description": "Capture sessions, upload, processing, frames, viewer."},
    {"name": "annotations", "description": "Issues pinned inside 360 frames."},
    {"name": "monitoring", "description": "Dashboards, timeline, coverage, capture frequency."},
    {"name": "reports", "description": "Report generation and download."},
    {"name": "cameras", "description": "Camera providers and registered devices."},
    {"name": "admin", "description": "Processing jobs, system capabilities, health."},
    {"name": "files", "description": "Signed asset delivery for the local storage backend."},
    {"name": "sync", "description": "Reference-data delta sync for offline-first clients."},
    {"name": "events", "description": "WebSocket push for capture processing status."},
    {
        "name": "shares",
        "description": (
            "Read-only links to a single view. Opening one needs no account — the token is the "
            "credential — so the shared payload is one frame with no navigation."
        ),
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(json_output=settings.environment != "development")
    logger.info(
        "Starting %s",
        settings.app_name,
        extra={
            "environment": settings.environment,
            "database": "postgresql" if not settings.is_sqlite else "sqlite",
            "storage_backend": settings.storage_backend,
            "queue_backend": settings.queue_backend,
        },
    )

    if settings.environment == "production":
        _warn_about_insecure_defaults()

    # Touch the queue at startup so the backend choice (and its warning) is visible immediately
    # rather than on the first upload.
    from app.queue import get_queue

    get_queue()

    # Sweep parts left by chunked uploads nobody finished. A deploy is a natural moment for it,
    # and on the filesystem backend nothing else would ever collect them.
    from app.storage.maintenance import purge_abandoned_uploads

    dropped = purge_abandoned_uploads(force=True)
    if dropped:
        logger.info("Cleared %d abandoned upload(s) from staging", dropped)

    yield

    from app.queue import reset_queue

    reset_queue()
    logger.info("Shutting down")


def _warn_about_insecure_defaults() -> None:
    insecure = [
        name
        for name, value in (
            ("JWT_SECRET", settings.jwt_secret),
            ("STORAGE_SIGNING_SECRET", settings.storage_signing_secret),
        )
        if value.startswith("dev-only-insecure")
    ]
    if insecure:
        logger.error(
            "Insecure default secrets are in use in production: %s. Set them in the "
            "environment before exposing this service.",
            ", ".join(insecure),
        )


app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    version="0.1.0",
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# Runs before routing and body parsing, which is the point: a limit that runs after the body has
# been read is not a limit on anything. It sits *inside* CORS rather than outside — Starlette's
# `add_middleware` inserts at the front of the list and the stack is built in reverse, so the
# last one added is the outermost. That is the better order anyway: a browser can only read the
# 413 if the CORS headers are on it.
app.add_middleware(
    BodySizeLimitMiddleware,
    default_limit=settings.max_json_body_bytes,
    upload_limit=settings.max_upload_bytes,
    api_prefix=settings.api_prefix,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Correlation-Id", "X-Response-Time-Ms", "Retry-After"],
)


@app.middleware("http")
async def correlation_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[JSONResponse]]
) -> JSONResponse:
    """Attach a correlation id and timing to every request/response (§45)."""
    incoming = request.headers.get("x-correlation-id")
    cid = incoming or uuid.uuid4().hex[:16]
    # Also stored on request.state: Starlette runs the catch-all 500 handler in
    # ServerErrorMiddleware, which sits *outside* this middleware, so by then the contextvar has
    # already been reset. Without this the error response could not name the correlation id that
    # the log entry carries — which is the only reason to return one.
    request.state.correlation_id = cid
    token = correlation_id.set(cid)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        correlation_id.reset(token)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Correlation-Id"] = cid
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
    return response


# Added last, so it is the outermost of the application's middleware and every response it wraps
# carries the headers — including the 413 the body limit writes by hand and the CORS preflight.
# The two documentation pages need a policy of their own; the paths come from the application
# rather than being spelled again here, so moving them moves the policy with them.
app.add_middleware(
    SecurityHeadersMiddleware,
    document_paths=frozenset(path for path in (app.docs_url, app.redoc_url) if path),
    hsts_max_age=settings.hsts_max_age_seconds,
)


# ---- error handling ----------------------------------------------------------
#: Path segments that are credentials rather than identifiers.
#:
#: A share token lives in the URL, which is what makes a link pasteable and also means it reaches
#: anywhere the path reaches. The two error handlers below log the path, so a 500 while opening a
#: link would write a *live* token into logs that are shipped further and kept longer than the
#: database is. The token is redacted before it gets there.
_REDACTED_PREFIXES = (f"{settings.api_prefix}/shared/",)


def safe_path(request: Request) -> str:
    """The request path, with any credential in it replaced rather than logged."""
    path = request.url.path
    for prefix in _REDACTED_PREFIXES:
        if path.startswith(prefix):
            return f"{prefix}<redacted>"
    return path


@app.exception_handler(SiteScopeError)
async def sitescope_error_handler(request: Request, exc: SiteScopeError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.exception("Unhandled domain error", extra={"path": safe_path(request)})
    # `Retry-After` is the standard way to say "wait this long", and the only one a generic HTTP
    # client will act on. The same number is in the body for a browser fetch, which cannot read
    # the header cross-origin unless it is explicitly exposed.
    headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimitedError) else None
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload(), headers=headers)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return field-level messages a form can actually display."""
    fields = [
        {
            "field": ".".join(str(part) for part in error["loc"][1:]) or str(error["loc"][0]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "The request could not be validated",
            "details": {"fields": fields},
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled exception", extra={"path": safe_path(request), "method": request.method}
    )
    # Never leak internals to a client; the correlation id ties the response to the log entry.
    cid = getattr(request.state, "correlation_id", None) or correlation_id.get()
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_error",
            "message": "An unexpected error occurred",
            "details": {"correlation_id": cid},
        },
        # The one response that does not pass back through the middleware stack: a handler for
        # `Exception` becomes `ServerErrorMiddleware`'s, which wraps everything the application
        # adds. Without this the 500 page would be the only response in the API with no security
        # headers on it, and "every response" is the claim worth being able to make.
        headers=dict(headers_for(document=False)),
    )


# ---- routes ------------------------------------------------------------------
prefix = settings.api_prefix
app.include_router(auth.router, prefix=prefix)
app.include_router(auth.users_router, prefix=prefix)
app.include_router(projects.router, prefix=prefix)
app.include_router(floorplans.router, prefix=prefix)
app.include_router(captures.router, prefix=prefix)
app.include_router(annotation_routes.router, prefix=prefix)
app.include_router(monitoring.router, prefix=prefix)
app.include_router(reports.router, prefix=prefix)
app.include_router(cameras.router, prefix=prefix)
app.include_router(admin.router, prefix=prefix)
app.include_router(sync.router, prefix=prefix)
app.include_router(shares.router, prefix=prefix)
app.include_router(events.router, prefix=prefix)
app.include_router(files.router, prefix=prefix)


@app.get("/", tags=["admin"])
def root() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "description": "Independent construction 360° site monitoring platform",
        "docs": "/docs",
        "api": prefix,
        "health": f"{prefix}/system/health",
    }


@app.get("/health", tags=["admin"])
def health() -> dict[str, str]:
    """Container liveness probe — no database access, so it stays up during DB blips."""
    return {"status": "ok"}


def _emit_ready() -> None:  # pragma: no cover
    log_event("api.ready", environment=settings.environment)
