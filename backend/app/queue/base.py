"""Job queue abstraction (§8, §25).

Two backends behind one protocol:

* ``RedisQueue``  — RQ + Redis. Durable, multi-worker. Used in Docker Compose and production.
* ``ThreadQueue`` — a bounded thread pool inside the API process. Used for local development and
  tests, where installing Redis would be the only thing standing between a developer and a
  working platform.

Both run the *same* handler functions and write the same ``processing_jobs`` rows, so behaviour
does not fork between environments. What the thread backend gives up is durability across a
restart — which is why it is not the production default, and why it says so on startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.core.enums import JobType


@dataclass(frozen=True, slots=True)
class JobSpec:
    """A unit of pipeline work. Must be JSON-serialisable to survive a queue round trip."""

    job_type: JobType
    session_id: str
    file_id: str | None = None
    #: Database id of the ``ProcessingJob`` row this execution belongs to.
    job_row_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_type": str(self.job_type),
            "session_id": self.session_id,
            "file_id": self.file_id,
            "job_row_id": self.job_row_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobSpec:
        return cls(
            job_type=JobType(data["job_type"]),
            session_id=data["session_id"],
            file_id=data.get("file_id"),
            job_row_id=data.get("job_row_id"),
            payload=data.get("payload") or {},
        )


class JobQueue(Protocol):
    name: str

    def enqueue(self, spec: JobSpec) -> str:
        """Submit work; return a backend job id. Must not block on execution."""
        ...

    def enqueue_chain(self, specs: list[JobSpec]) -> list[str]:
        """Submit a dependent sequence — each runs only if its predecessor succeeded."""
        ...

    def is_abandoned(self, queue_job_id: str | None, started_at: datetime | None) -> bool:
        """Is this job definitely not being worked on any more?

        Asked about a ``ProcessingJob`` row that still says RUNNING. A worker killed mid-stage
        leaves that row behind, and nothing else ever reconsiders it: the capture shows a
        spinner for ever, `/retry` refuses because the session is `processing` rather than
        `failed`, and the UI renders no button at all. The only way out was a force reprocess
        through the API by hand.

        Elapsed time cannot answer it. A stage commits RUNNING and then does not touch the row
        again until it finishes, so a long video extraction and a dead worker look identical
        from the outside. The queue is the only thing that knows, so the queue is asked.

        **False means "alive, or cannot tell."** The two are deliberately the same answer: this
        decision fails a capture and tells an operator their work needs rerunning, so it is
        taken only on a definite no.
        """
        ...

    def health(self) -> dict[str, Any]: ...

    def close(self) -> None: ...
