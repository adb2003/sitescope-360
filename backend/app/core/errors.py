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


class UnsupportedMediaError(SiteScopeError):
    status_code = 415
    code = "unsupported_media"


class PayloadTooLargeError(SiteScopeError):
    status_code = 413
    code = "payload_too_large"


class ProcessingError(SiteScopeError):
    """Raised inside a pipeline stage; recorded on the ProcessingJob row."""

    status_code = 500
    code = "processing_failed"
