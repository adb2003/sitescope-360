"""Server-to-client push, so the web app stops asking (§34).

The processing status page polls every twenty seconds. That is correct and it is slow in both
directions: an operator watching a capture finish waits up to twenty seconds to be told, and
every open tab asks a question whose answer is almost always "nothing has changed". A pipeline
stage that takes four seconds is invisible; a five-minute walk looks stalled.

So the pipeline announces what it did, and the announcement reaches the browser.

**Two backends, and the choice is not free.** The worker is a *separate process* under
``QUEUE_BACKEND=redis``, so it cannot reach the API process's open sockets — the message has to
travel through something both processes can see, which is the Redis that is already a dependency
for the queue. Under ``QUEUE_BACKEND=thread`` the worker is a thread of the API process and an
in-process fan-out is not only sufficient but better: no broker, so the whole chain still runs on
a laptop with nothing installed.

Which is why the bus follows :attr:`~app.core.config.Settings.queue_backend` rather than having a
setting of its own. The broken combination — a Redis queue with an in-process bus — publishes
into the worker's own memory, where nobody is listening, and *looks* like it works: the pipeline
runs, the status is written to the database, the poll eventually picks it up. Nothing errors and
the feature is silently absent. Deriving it removes the option.

**Push is an optimisation, never the source of truth.** A dropped socket, a missed message, a
client that was closed — all of them have to be survivable, so the database stays authoritative
and the client keeps polling as a floor. This makes the status fast; it does not make it correct.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from app.core.logging import get_logger

logger = get_logger(__name__)

#: One channel for everything. Every signed-in user of this deployment can already read every
#: project (there is no per-project scoping in the authorisation model), so a shared channel
#: leaks nothing that a `GET /projects` would not. The moment project scoping exists this has to
#: become per-project rooms, and a subscriber filter is not a substitute -- the message would
#: still have been delivered to the client.
CHANNEL = "sitescope:events"

#: How many messages a slow client may fall behind before it is dropped.
#:
#: A browser tab that has been backgrounded for an hour must not become unbounded memory in the
#: API process. Dropping is safe *because* the client polls: it loses the notifications, not the
#: state.
SUBSCRIBER_BACKLOG = 256


@dataclass(frozen=True, slots=True)
class Event:
    """Something worth telling a client about, right now."""

    type: str
    #: Everything else. Deliberately loose: an event is a hint to refetch, not a replacement for
    #: the resource, so a client that does not recognise a field can ignore it.
    data: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "at": self.at.isoformat(), **self.data})

    @staticmethod
    def from_json(raw: str) -> Event:
        payload = dict(json.loads(raw))
        event_type = str(payload.pop("type", "unknown"))
        at_raw = payload.pop("at", None)
        at = datetime.now(UTC)
        if isinstance(at_raw, str):
            with contextlib.suppress(ValueError):
                at = datetime.fromisoformat(at_raw)
        return Event(type=event_type, data=payload, at=at)


@runtime_checkable
class Subscription(Protocol):
    """An open subscription. Async-iterable, and closeable while idle.

    Deliberately not an async generator, which was the first shape this took. A generator
    registers nothing until its consumer first iterates it, so any event published between
    ``subscribe()`` and that first ``__anext__`` goes to nobody — and it cannot be closed while
    a pending ``__anext__`` exists, which is the normal state of a socket that is waiting. Both
    problems disappear once subscribing is a thing that *happens* rather than a thing that is
    promised.
    """

    def __aiter__(self) -> Subscription: ...

    async def __anext__(self) -> Event: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class EventBus(Protocol):
    name: str

    def publish(self, event: Event) -> None:
        """Announce an event. Callable from any thread, and from a worker process.

        Must never raise. A pipeline stage that completed successfully cannot be turned into a
        failure because a notification could not be delivered — the state is already committed,
        and the client's poll will find it.
        """
        ...

    async def subscribe(self) -> Subscription:
        """Start receiving events. Everything published after this returns is delivered."""
        ...


class MemorySubscription:
    """One in-process listener's queue, registered for as long as it is open."""

    def __init__(self, bus: MemoryEventBus, loop: asyncio.AbstractEventLoop) -> None:
        self._bus = bus
        self.loop = loop
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=SUBSCRIBER_BACKLOG)
        self._closed = False

    def __aiter__(self) -> MemorySubscription:
        return self

    async def __anext__(self) -> Event:
        if self._closed:
            raise StopAsyncIteration
        return await self.queue.get()

    async def aclose(self) -> None:
        self._closed = True
        self._bus.remove(self)


class MemoryEventBus:
    """In-process fan-out, for the single-process path.

    The subtlety is threading. Publishers are synchronous and may be on any thread — the
    ThreadQueue worker is exactly that — while subscribers are asyncio consumers on the event
    loop. ``call_soon_threadsafe`` is the handoff, and the loop each subscriber belongs to is
    captured when it subscribes rather than assumed to be the current one.
    """

    name = "memory"

    def __init__(self) -> None:
        self._subscribers: set[MemorySubscription] = set()

    def publish(self, event: Event) -> None:
        for subscriber in list(self._subscribers):
            try:
                if subscriber.loop.is_closed():
                    continue
                subscriber.loop.call_soon_threadsafe(self._offer, subscriber.queue, event)
            except RuntimeError:
                # The loop shut down between the check and the call. Nothing to deliver to.
                continue

    @staticmethod
    def _offer(queue: asyncio.Queue[Event], event: Event) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Dropping an event for a subscriber that is not keeping up")

    async def subscribe(self) -> MemorySubscription:
        subscription = MemorySubscription(self, asyncio.get_running_loop())
        self._subscribers.add(subscription)
        return subscription

    def remove(self, subscription: MemorySubscription) -> None:
        self._subscribers.discard(subscription)

    @property
    def subscriber_count(self) -> int:
        """For tests and for a health check: a number that must come back down."""
        return len(self._subscribers)


class RedisEventBus:
    """Redis pub/sub, so a worker in another process can be heard.

    Two clients, and they cannot be shared. Publishing happens in synchronous worker code, while
    subscribing is an async consumer inside the API's event loop — one blocking client and one
    ``redis.asyncio`` client. Sharing either across that boundary blocks the loop or fails
    outright, and the failure would be intermittent rather than obvious.
    """

    name = "redis"

    def __init__(self, url: str | None = None) -> None:
        from app.core.config import settings

        self._url = url or settings.redis_url
        self._sync: Any | None = None

    def _publisher(self) -> Any:
        if self._sync is None:
            from redis import Redis

            self._sync = Redis.from_url(self._url)
        return self._sync

    def publish(self, event: Event) -> None:
        try:
            self._publisher().publish(CHANNEL, event.to_json())
        except Exception:
            # Never fatal: see the protocol. A stage that finished must not be reported as failed
            # because the broker was briefly unreachable.
            logger.warning("Could not publish %s; clients will fall back to polling", event.type)

    async def subscribe(self) -> RedisSubscription:
        from redis.asyncio import Redis as AsyncRedis

        client = AsyncRedis.from_url(self._url)
        pubsub = client.pubsub()
        # Awaited here rather than on first iteration, so an event published immediately after
        # this returns is genuinely delivered.
        await pubsub.subscribe(CHANNEL)
        return RedisSubscription(client, pubsub)


class RedisSubscription:
    """One client's Redis pub/sub connection, and its own clean-up."""

    def __init__(self, client: Any, pubsub: Any) -> None:
        self._client = client
        self._pubsub = pubsub

    def __aiter__(self) -> RedisSubscription:
        return self

    async def __anext__(self) -> Event:
        while True:
            message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
            if message is None or message.get("type") != "message":
                continue
            raw = message.get("data")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            if not isinstance(raw, str):
                continue
            try:
                return Event.from_json(raw)
            except (ValueError, TypeError):
                # Something else is publishing on this channel. Skipping one message is better
                # than tearing down every client's connection.
                logger.warning("Ignoring an unreadable event on %s", CHANNEL)

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._pubsub.unsubscribe(CHANNEL)
        with contextlib.suppress(Exception):
            await self._pubsub.close()
        with contextlib.suppress(Exception):
            await self._client.aclose()
