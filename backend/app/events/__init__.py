"""The event bus, and the events this application actually publishes.

`get_event_bus` is a process-wide singleton, chosen from the queue backend rather than from a
setting of its own — see `bus.py` for why the wrong combination is worse than no push at all.
"""

from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.events.bus import CHANNEL, Event, EventBus, MemoryEventBus, RedisEventBus

logger = get_logger(__name__)

_bus: EventBus | None = None

#: Emitted whenever a capture session's processing state changes.
#:
#: One event type rather than one per transition, because every client does the same thing with
#: it: refetch the session. `status` carries which transition it was, so a UI that wants to say
#: "extracting frames" can, without the server having to predict that it would.
CAPTURE_STATUS = "capture.status"


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        if settings.queue_backend == "redis":
            try:
                _bus = RedisEventBus()
            except Exception:
                # Redis is the queue's dependency, so this is nearly unreachable -- but a bus
                # that cannot start must not stop the API from serving. Clients poll.
                logger.warning("Falling back to the in-process event bus; push will be local only")
                _bus = MemoryEventBus()
        else:
            _bus = MemoryEventBus()
        logger.info("Event bus: %s", _bus.name)
    return _bus


def reset_event_bus() -> None:
    """Drop the singleton. For tests that switch backends."""
    global _bus
    _bus = None


def publish_capture_status(
    session_id: str,
    status: str,
    *,
    stage: str | None = None,
    progress: float | None = None,
    error: str | None = None,
) -> None:
    """Tell any watching client that a capture moved.

    Swallows everything. This is called from inside a pipeline stage that has already committed
    its work, so raising here would convert a completed stage into a failed one and cost a real
    reprocessing run to deliver a notification nobody is waiting on any more.
    """
    data: dict[str, object] = {"session_id": session_id, "status": status}
    if stage is not None:
        data["stage"] = stage
    if progress is not None:
        data["progress"] = progress
    if error is not None:
        data["error"] = error
    try:
        get_event_bus().publish(Event(type=CAPTURE_STATUS, data=data))
    except Exception:
        logger.warning("Could not publish a status event for %s", session_id)


__all__ = [
    "CAPTURE_STATUS",
    "CHANNEL",
    "Event",
    "EventBus",
    "MemoryEventBus",
    "RedisEventBus",
    "get_event_bus",
    "publish_capture_status",
    "reset_event_bus",
]
