"""In-process thread-pool queue for local development and tests.

Chains are important: the pipeline is sequential, and a stage must not start if its predecessor
failed. A plain ``ThreadPoolExecutor.submit`` per stage would run them concurrently, so a chain
is submitted as a *single* task that walks the stages in order and stops on the first failure —
the same semantics RQ gives via job dependencies.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger, new_correlation_id
from app.queue.base import JobSpec

logger = get_logger(__name__)

#: Set by ``app.workers.handlers`` at import time to avoid a circular import.
HandlerFn = Callable[[JobSpec], dict[str, Any]]
_handler: HandlerFn | None = None


def set_handler(handler: HandlerFn) -> None:
    global _handler
    _handler = handler


def _run(spec: JobSpec) -> dict[str, Any]:
    if _handler is None:
        raise RuntimeError("No job handler registered — import app.workers.handlers first")
    return _handler(spec)


class ThreadQueue:
    name = "thread"

    def __init__(self, workers: int | None = None) -> None:
        self._workers = int(workers or settings.thread_queue_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=self._workers, thread_name_prefix="sitescope-job"
        )
        self._lock = threading.Lock()
        self._futures: dict[str, Future[Any]] = {}
        #: When this queue came into existence. Work submitted to a previous instance cannot
        #: still be running, because there is no previous instance — see `is_abandoned`.
        self._created_at = datetime.now(UTC)

    def enqueue(self, spec: JobSpec) -> str:
        return self.enqueue_chain([spec])[0]

    def enqueue_chain(self, specs: list[JobSpec]) -> list[str]:
        if not specs:
            return []
        ids = [f"thread-{uuid.uuid4().hex[:12]}" for _ in specs]
        correlation = new_correlation_id()
        future = self._executor.submit(self._run_chain, specs, ids, correlation)
        with self._lock:
            self._futures[ids[0]] = future
            # Bound the bookkeeping: this is a dev backend, not an audit log.
            if len(self._futures) > 512:
                for key in [k for k, f in list(self._futures.items()) if f.done()][:256]:
                    self._futures.pop(key, None)
        return ids

    def _run_chain(
        self, specs: list[JobSpec], ids: list[str], correlation: str
    ) -> list[dict[str, Any]]:
        from app.core.logging import correlation_id

        correlation_id.set(correlation)
        results: list[dict[str, Any]] = []
        for spec in specs:
            try:
                results.append(_run(spec))
            except Exception:
                logger.exception(
                    "Pipeline chain aborted",
                    extra={"job_type": str(spec.job_type), "session_id": spec.session_id},
                )
                # The handler has already recorded the failure on the ProcessingJob row and
                # marked the session failed; downstream stages are skipped.
                break
        return results

    def is_abandoned(self, queue_job_id: str | None, started_at: datetime | None) -> bool:
        """A job that started before this queue existed is not running in it.

        Exact rather than a guess, and the one thing this backend can be certain about: the
        work lives in a thread pool inside the API process, so a restart loses it by
        definition. The futures dict cannot answer instead — it keys only the head of each
        chain and prunes itself — but it does not need to.
        """
        if started_at is None:
            return False
        return started_at < self._created_at

    def health(self) -> dict[str, Any]:
        with self._lock:
            pending = sum(1 for f in self._futures.values() if not f.done())
        return {
            "backend": self.name,
            "workers": self._workers,
            "pending_chains": pending,
            "durable": False,
            "note": "In-process queue: queued work is lost if the API restarts.",
        }

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def wait_idle(self, timeout: float = 120.0) -> bool:
        """Block until submitted chains finish. Tests use this; request handlers must not."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                futures = [f for f in self._futures.values() if not f.done()]
            if not futures:
                return True
            for future in futures:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                with contextlib.suppress(Exception):
                    future.result(timeout=remaining)
        with self._lock:
            return all(f.done() for f in self._futures.values())
