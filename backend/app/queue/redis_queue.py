"""RQ + Redis queue backend — the production path (§25)."""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.queue.base import JobSpec

logger = get_logger(__name__)

#: Long enough for a 20-minute 360 video, short enough that a wedged job is noticed.
JOB_TIMEOUT_SECONDS = 3600
RESULT_TTL_SECONDS = 86400


def execute(spec_dict: dict[str, Any]) -> dict[str, Any]:
    """Module-level entrypoint RQ can import by dotted path in the worker process."""
    from app.workers.handlers import handle_job

    return handle_job(JobSpec.from_dict(spec_dict))


class RedisQueue:
    name = "redis"

    def __init__(self, url: str | None = None, queue_name: str | None = None) -> None:
        from redis import Redis
        from rq import Queue

        self._connection = Redis.from_url(url or settings.redis_url)
        self._queue = Queue(
            queue_name or settings.queue_name,
            connection=self._connection,
            default_timeout=JOB_TIMEOUT_SECONDS,
        )

    def enqueue(self, spec: JobSpec) -> str:
        job = self._queue.enqueue(
            execute,
            spec.to_dict(),
            job_timeout=JOB_TIMEOUT_SECONDS,
            result_ttl=RESULT_TTL_SECONDS,
            description=f"{spec.job_type}:{spec.session_id}",
        )
        return str(job.id)

    def enqueue_chain(self, specs: list[JobSpec]) -> list[str]:
        """Enqueue with RQ dependencies so a failed stage stops the rest."""
        ids: list[str] = []
        previous = None
        for spec in specs:
            job = self._queue.enqueue(
                execute,
                spec.to_dict(),
                depends_on=previous,
                job_timeout=JOB_TIMEOUT_SECONDS,
                result_ttl=RESULT_TTL_SECONDS,
                description=f"{spec.job_type}:{spec.session_id}",
            )
            ids.append(str(job.id))
            previous = job
        return ids

    def is_abandoned(self, queue_job_id: str | None, started_at: datetime | None) -> bool:
        """Ask RQ. It knows whether the job is still queued, started, or gone.

        A worker killed mid-job leaves RQ's record in a terminal state or lets it expire
        entirely; either way RQ stops calling it live, and that is authoritative in a way no
        timeout would be. A connection failure answers False — unknown is not dead.
        """
        if not queue_job_id:
            return False
        try:
            from rq.exceptions import NoSuchJobError
            from rq.job import Job

            try:
                job = Job.fetch(queue_job_id, connection=self._connection)
            except NoSuchJobError:
                return True
            live = {"queued", "started", "deferred", "scheduled"}
            return job.get_status(refresh=True) not in live
        except Exception:
            # Redis unreachable, or a version whose API has moved. Either way this is not
            # evidence that the work stopped.
            return False

    def health(self) -> dict[str, Any]:
        try:
            self._connection.ping()
            return {
                "backend": self.name,
                "connected": True,
                "queued": self._queue.count,
                "durable": True,
            }
        except Exception as exc:
            return {"backend": self.name, "connected": False, "error": str(exc)}

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._connection.close()
