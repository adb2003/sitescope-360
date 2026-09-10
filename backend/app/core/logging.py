"""Structured JSON logging with request/job correlation ids (§45).

Domain events are emitted through :func:`log_event` so the observable event names live in one
place and stay greppable:

    capture.created · file.uploaded · processing.started · processing.stage.completed
    processing.failed · processing.completed · mapping.completed · viewer.opened
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_RESERVED = frozenset(
    [
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    ]
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        cid = correlation_id.get()
        if cid:
            payload["correlation_id"] = cid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _safe(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _safe(value: Any) -> Any:
    if isinstance(value, str | int | float | bool | type(None) | list | dict):
        return value
    return str(value)


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s :: %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).handlers = [handler]
        logging.getLogger(noisy).propagate = False


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


_event_log = logging.getLogger("sitescope.event")


def log_event(event: str, **fields: Any) -> None:
    """Emit one structured domain event.

    Field names are sanitised first. ``logging`` raises ``KeyError`` if an ``extra`` key shadows a
    built-in ``LogRecord`` attribute, and several natural field names do — ``filename``,
    ``module``, ``name``, ``message``. Rather than making every call site remember that, a
    colliding key is prefixed here, so ``log_event("file.uploaded", filename=...)`` works and
    arrives in the log as ``event_filename``.
    """
    _event_log.info(event, extra=safe_extra({"event": event, **fields}))


def safe_extra(fields: dict[str, Any]) -> dict[str, Any]:
    """Rename any field that would collide with a reserved ``LogRecord`` attribute."""
    return {
        (f"event_{key}" if key in _RESERVED or key == "asctime" else key): value
        for key, value in fields.items()
    }
