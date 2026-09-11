"""Container healthcheck for the worker service.

    python -m app.workers.healthcheck

The worker shares ``backend/Dockerfile`` with the API, and that image's ``HEALTHCHECK`` polls
``http://127.0.0.1:8000/health``. The API serves that; the worker runs :mod:`app.workers.run` and
has no HTTP server at all, so it inherited a probe it could never satisfy. ``docker compose up
--wait`` therefore hung on the worker and failed the stack after every other service came up
healthy.

Disabling the inherited probe would have been enough to make CI pass, which is exactly why it is
the wrong fix: a worker that has died, lost Redis, or wedged mid-job is the failure this stack
most needs to notice, and "no healthcheck" reports a dead worker as healthy. So this asks the
question that actually matters — *is my RQ worker registered and heartbeating?*
"""

from __future__ import annotations

import datetime
import socket
import sys

from app.core.config import settings

#: How stale a heartbeat may be before the worker is considered wedged.
#:
#: RQ refreshes the heartbeat every ``job_monitoring_interval`` (30 s by default) even while a long
#: job is running, so 120 s allows three missed refreshes. Tighter than RQ's own ``worker_ttl``
#: (420 s), because the key surviving only proves the worker died recently, not that it is alive.
MAX_HEARTBEAT_AGE_SECONDS = 120.0


def main() -> int:
    if settings.queue_backend != "redis":
        # Under the thread backend the API runs the pool in-process and `app.workers.run` exits
        # immediately by design. There is no worker to check, and claiming otherwise would be a
        # false negative on a correctly configured deployment.
        return 0

    from redis import Redis
    from rq import Queue, Worker

    try:
        connection = Redis.from_url(settings.redis_url)
        connection.ping()
    except Exception as exc:
        print(f"unhealthy: cannot reach Redis at {settings.redis_url}: {exc}", file=sys.stderr)
        return 1

    queue = Queue(settings.queue_name, connection=connection)
    hostname = socket.gethostname()

    # Filter to this container's own worker. `Worker.all` returns every worker on the queue, and
    # accepting any of them would report all replicas healthy while this one is dead.
    try:
        mine = [
            worker
            for worker in Worker.all(connection=connection, queue=queue)
            if worker.hostname == hostname
        ]
    except Exception as exc:
        print(f"unhealthy: could not read the worker registry: {exc}", file=sys.stderr)
        return 1

    if not mine:
        print(
            f"unhealthy: no RQ worker registered for host {hostname} on queue "
            f"{settings.queue_name!r}",
            file=sys.stderr,
        )
        return 1

    heartbeats = [worker.last_heartbeat for worker in mine if worker.last_heartbeat is not None]
    if not heartbeats:
        print(f"unhealthy: worker on {hostname} has never sent a heartbeat", file=sys.stderr)
        return 1

    age = (datetime.datetime.now(datetime.UTC) - max(heartbeats)).total_seconds()
    if age > MAX_HEARTBEAT_AGE_SECONDS:
        print(
            f"unhealthy: worker heartbeat is {age:.0f}s old "
            f"(limit {MAX_HEARTBEAT_AGE_SECONDS:.0f}s)",
            file=sys.stderr,
        )
        return 1

    print(f"healthy: worker on {hostname}, heartbeat {age:.0f}s ago")
    return 0


if __name__ == "__main__":
    sys.exit(main())
