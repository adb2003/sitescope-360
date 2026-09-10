"""Worker entrypoint.

    python -m app.workers.run

With ``QUEUE_BACKEND=redis`` this starts an RQ worker. With the default thread backend there is
nothing to start — the API process runs the pool itself — so the command explains that instead of
idling silently and looking healthy while doing nothing.
"""

from __future__ import annotations

import sys

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("sitescope.worker")


def main() -> int:
    configure_logging(json_output=settings.environment != "development")

    # Registers stage handlers and makes app.queue.redis_queue.execute importable.
    import app.workers.handlers  # noqa: F401

    if settings.queue_backend != "redis":
        logger.warning(
            "QUEUE_BACKEND=%s — the in-process thread pool inside the API handles jobs, so no "
            "separate worker is needed. Set QUEUE_BACKEND=redis to run a real worker.",
            settings.queue_backend,
        )
        return 0

    from redis import Redis
    from rq import Queue, Worker

    connection = Redis.from_url(settings.redis_url)
    try:
        connection.ping()
    except Exception:
        logger.exception("Cannot reach Redis at %s", settings.redis_url)
        return 1

    queue = Queue(settings.queue_name, connection=connection)
    logger.info(
        "Worker starting", extra={"queue": settings.queue_name, "redis_url": settings.redis_url}
    )
    Worker([queue], connection=connection).work(with_scheduler=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
