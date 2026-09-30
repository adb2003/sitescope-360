"""Domain exceptions mapped to HTTP responses in ``app.main``."""

from __future__ import annotations

from typing import Any


class SiteScopeError(Exception):
    """Base class. ``status_code`` and ``code`` drive the HTTP response."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class NotFoundError(SiteScopeError):
    status_code = 404
    code = "not_found"


class ValidationError(SiteScopeError):
    status_code = 422
    code = "validation_error"


class ConflictError(SiteScopeError):
    status_code = 409
    code = "conflict"


class AuthenticationError(SiteScopeError):
    status_code = 401
    code = "authentication_failed"


class PermissionDeniedError(SiteScopeError):
    status_code = 403
    code = "permission_denied"


class PasswordChangeRequiredError(SiteScopeError):
    """The credential is valid; the account may not use it for anything else yet.

    403 and not 401: the token is genuine and re-authenticating with it would change nothing, so
    a client that retries a 401 by refreshing would loop. The distinct ``code`` is what a client
    branches on — it means "send them to the change-password screen", not "sign them in again".
    """

    status_code = 403
    code = "password_change_required"


class UnsupportedMediaError(SiteScopeError):
    status_code = 415
    code = "unsupported_media"


class PayloadTooLargeError(SiteScopeError):
    status_code = 413
    code = "payload_too_large"


class RateLimitedError(SiteScopeError):
    """Too many attempts. Carries how long to wait, so a client need not guess.

    ``retry_after`` is surfaced as the HTTP header of the same name as well as in the body: a
    browser fetch reads the body, and anything scripted reads the header.
    """

    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after: int, **details: Any) -> None:
        super().__init__(message, retry_after=retry_after, **details)
        self.retry_after = max(1, int(retry_after))


class ProcessingError(SiteScopeError):
    """Raised inside a pipeline stage; recorded on the ProcessingJob row."""

    status_code = 500
    code = "processing_failed"
