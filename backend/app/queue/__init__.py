"""Queue backend registry."""

from __future__ import annotations

import threading

from app.core.config import settings
from app.core.logging import get_logger
from app.queue.base import JobQueue, JobSpec

logger = get_logger(__name__)

_queue: JobQueue | None = None
_lock = threading.Lock()


def get_queue() -> JobQueue:
    global _queue
    if _queue is not None:
        return _queue
    with _lock:
        if _queue is None:
            _queue = _build()
    return _queue


def _build() -> JobQueue:
    # Importing handlers registers the ThreadQueue callback and makes the RQ entrypoint
    # importable; doing it here keeps app.queue free of a circular import at module scope.
    import app.workers.handlers  # noqa: F401

    if settings.queue_backend == "redis":
        from app.queue.redis_queue import RedisQueue

        logger.info("Using Redis (RQ) job queue", extra={"redis_url": settings.redis_url})
        return RedisQueue()

    from app.queue.thread import ThreadQueue

    logger.warning(
        "Using in-process thread job queue — queued work is lost on restart. "
        "Set QUEUE_BACKEND=redis for durable processing.",
        extra={"workers": settings.thread_queue_workers},
    )
    return ThreadQueue()


def reset_queue() -> None:
    """Drop the cached queue. Used by tests."""
    global _queue
    with _lock:
        if _queue is not None:
            _queue.close()
        _queue = None


__all__ = ["JobQueue", "JobSpec", "get_queue", "reset_queue"]
