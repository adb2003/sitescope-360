"""WebSocket push for processing status (§34).

Push is an optimisation over the twenty-second poll, which means most of what can go wrong here
is *invisible*: a socket that authenticates nobody, a subscriber that is never removed, an event
announced before the database was told. None of them break a page — the poll still arrives — so
none of them would be noticed without being asserted.

The two that would be noticed are the ones worth stating plainly: a socket that leaks
subscribers turns a long-lived API process into a memory leak that grows with every browser tab
ever opened, and a socket that accepts an unauthenticated client hands capture activity to
anyone who can reach the port.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.events import CAPTURE_STATUS, publish_capture_status, reset_event_bus
from app.events.bus import SUBSCRIBER_BACKLOG, Event, MemoryEventBus
from tests.conftest import auth_headers


def _token(client: TestClient, email: str) -> str:
    return auth_headers(client, email)["Authorization"].removeprefix("Bearer ")


# ---- the bus ---------------------------------------------------------------------------------
# These are coroutines driven by `asyncio.run`, because the bus is asyncio on the subscriber side
# and synchronous on the publisher side. Running them this way keeps the install free of a pytest
# async plugin it has managed without.
def _run(scenario: object) -> None:
    asyncio.run(scenario)  # type: ignore[arg-type]


def test_a_subscriber_receives_what_is_published() -> None:
    async def scenario() -> None:
        bus = MemoryEventBus()
        stream = await bus.subscribe()

        bus.publish(Event(type="capture.status", data={"session_id": "CAP-1"}))

        received = await asyncio.wait_for(anext(stream), timeout=2)
        assert received.type == "capture.status"
        assert received.data == {"session_id": "CAP-1"}
        await stream.aclose()

    _run(scenario())


def test_an_event_published_immediately_after_subscribing_is_not_lost() -> None:
    """Subscribing happens when `subscribe()` returns, not when iteration starts.

    The first version of this bus was an async generator, which registers nothing until its
    consumer first awaits it — so an event published in between went to nobody. On a socket that
    window is small and real: the client is told "ready" and a stage can complete before the
    `async for` has run once.
    """

    async def scenario() -> None:
        bus = MemoryEventBus()
        stream = await bus.subscribe()
        assert bus.subscriber_count == 1, "subscribing is not effective until iteration"

        # Published before anything reads. It has to be queued, not dropped.
        bus.publish(Event(type="capture.status", data={"session_id": "CAP-EARLY"}))

        received = await asyncio.wait_for(anext(stream), timeout=2)
        assert received.data["session_id"] == "CAP-EARLY"
        await stream.aclose()

    _run(scenario())


def test_every_subscriber_receives_the_same_event() -> None:
    async def scenario() -> None:
        bus = MemoryEventBus()
        first = await bus.subscribe()
        second = await bus.subscribe()

        bus.publish(Event(type="capture.status", data={"session_id": "CAP-2"}))

        for stream in (first, second):
            received = await asyncio.wait_for(anext(stream), timeout=2)
            assert received.data["session_id"] == "CAP-2"
        await first.aclose()
        await second.aclose()

    _run(scenario())


def test_closing_a_subscription_removes_it() -> None:
    """The leak that would grow with every browser tab ever opened.

    A subscriber is a queue the bus writes into. If closing a socket does not remove it, the
    process accumulates one per connection for ever, each still being filled by every event.
    """

    async def scenario() -> None:
        bus = MemoryEventBus()
        stream = await bus.subscribe()
        assert bus.subscriber_count == 1

        await stream.aclose()

        assert bus.subscriber_count == 0

    _run(scenario())


def test_publishing_from_another_thread_reaches_the_loop() -> None:
    """The ThreadQueue worker is exactly this: a synchronous publisher off the event loop.

    `Queue.put_nowait` from a foreign thread is not safe and, worse, usually *appears* to work —
    so this asserts the `call_soon_threadsafe` handoff rather than trusting it.
    """

    async def scenario() -> None:
        bus = MemoryEventBus()
        stream = await bus.subscribe()

        await asyncio.to_thread(bus.publish, Event(type="capture.status", data={"session_id": "T"}))

        received = await asyncio.wait_for(anext(stream), timeout=2)
        assert received.data["session_id"] == "T"
        await stream.aclose()

    _run(scenario())


def test_a_subscriber_that_stops_reading_is_dropped_rather_than_unbounded() -> None:
    """A backgrounded tab must not become unbounded memory in the API process.

    Dropping is safe precisely because the client polls: it loses the notifications, not the
    state.
    """

    async def scenario() -> None:
        bus = MemoryEventBus()
        stream = await bus.subscribe()

        for index in range(SUBSCRIBER_BACKLOG + 50):
            bus.publish(Event(type="capture.status", data={"n": index}))
        # The publishes are queued onto the loop; let them run.
        await asyncio.sleep(0)

        assert stream.queue.qsize() <= SUBSCRIBER_BACKLOG
        await stream.aclose()

    _run(scenario())


def test_an_event_survives_a_round_trip_through_json() -> None:
    # Redis carries strings, so anything that does not survive this is silently lost in the one
    # deployment that needs a broker at all.
    original = Event(type=CAPTURE_STATUS, data={"session_id": "CAP-3", "progress": 0.5})

    restored = Event.from_json(original.to_json())

    assert restored.type == original.type
    assert restored.data == original.data
    assert restored.at == original.at


def test_publishing_never_raises_even_with_a_broken_bus(monkeypatch) -> None:
    """A stage that finished must not be reported as failed because a notification failed.

    The work is already committed by the time this is called, so raising would turn a successful
    five-minute pipeline run into a failure the operator has to retry — to deliver a message
    nobody is waiting for any more.
    """

    class Broken:
        name = "broken"

        def publish(self, event: Event) -> None:
            raise RuntimeError("no broker")

        def subscribe(self):  # pragma: no cover - never reached
            raise RuntimeError("no broker")

    monkeypatch.setattr("app.events._bus", Broken())
    publish_capture_status("CAP-4", "ready", project_id="P-4")  # must not raise


# ---- the socket ------------------------------------------------------------------------------
def test_a_client_that_authenticates_is_told_it_is_ready(client: TestClient, admin) -> None:
    reset_event_bus()
    token = _token(client, admin.email)

    with client.websocket_connect("/api/events") as socket:
        socket.send_text(json.dumps({"type": "auth", "token": token}))

        assert json.loads(socket.receive_text()) == {"type": "ready"}


def test_an_authenticated_socket_receives_a_published_event(client: TestClient, admin) -> None:
    reset_event_bus()
    token = _token(client, admin.email)

    with client.websocket_connect("/api/events") as socket:
        socket.send_text(json.dumps({"type": "auth", "token": token}))
        assert json.loads(socket.receive_text())["type"] == "ready"

        publish_capture_status("CAP-20260916-EVT", "ready", project_id="P-EVT", progress=1.0)

        message = json.loads(socket.receive_text())
        assert message["type"] == CAPTURE_STATUS
        assert message["session_id"] == "CAP-20260916-EVT"
        assert message["status"] == "ready"


def test_a_socket_with_no_token_is_closed(client: TestClient, admin) -> None:
    """Refused, not left open and silent.

    A connection that is accepted and then never sends anything is indistinguishable from a
    deployment where the pipeline is simply idle — so the failure has to be a close, with a code
    that says it was policy and not the network.
    """
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/api/events") as socket,
    ):
        socket.send_text(json.dumps({"type": "hello"}))
        socket.receive_text()

    assert closed.value.code == 1008


def test_a_socket_with_a_bad_token_is_closed(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/api/events") as socket,
    ):
        socket.send_text(json.dumps({"type": "auth", "token": "not-a-jwt"}))
        socket.receive_text()

    assert closed.value.code == 1008


def test_a_rejected_socket_never_receives_an_event(client: TestClient, admin) -> None:
    """The check that makes the auth step mean something.

    Subscribing before the token arrived would deliver events to an unauthenticated peer for as
    long as it stayed connected — and the code would still pass a test that only asserted the
    close.
    """
    from starlette.websockets import WebSocketDisconnect

    reset_event_bus()
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/events") as socket,
    ):
        socket.send_text(json.dumps({"type": "auth", "token": "nope"}))
        publish_capture_status("CAP-SECRET", "ready", project_id="P-SECRET")
        # Whatever comes back must be the close, never the event.
        received = socket.receive_text()
        assert "CAP-SECRET" not in received


def test_a_socket_hears_nothing_about_projects_its_owner_cannot_open(
    client: TestClient, admin, admin_headers, viewer
) -> None:
    """The leak this filter closes (§27).

    Every authenticated socket used to receive every event in the deployment — a session id, its
    status and its stage. A subcontractor's viewer could watch the main contractor's captures
    being processed without being able to open one, and the event payload is enough to infer how
    much work is going on and when.
    """
    from tests.conftest import join_project

    reset_event_bus()
    mine = client.post(
        "/api/projects", json={"name": "Heard", "code": "EVT-MINE"}, headers=admin_headers
    ).json()
    join_project(client, admin_headers, mine["id"], viewer.id)
    token = _token(client, viewer.email)

    with client.websocket_connect("/api/events") as socket:
        socket.send_text(json.dumps({"type": "auth", "token": token}))
        assert json.loads(socket.receive_text())["type"] == "ready"

        # Another project's capture first, then one of theirs. The first must be skipped rather
        # than queued: if it were merely delayed, the assertion below would pass by accident.
        publish_capture_status("CAP-THEIRS", "ready", project_id="some-other-project")
        publish_capture_status("CAP-MINE", "ready", project_id=mine["id"])

        message = json.loads(socket.receive_text())
        assert message["session_id"] == "CAP-MINE"
        assert message["project_id"] == mine["id"]


def test_an_administrators_socket_hears_everything(
    client: TestClient, admin, admin_headers
) -> None:
    """The same exemption the HTTP routes make, for the same reason."""
    reset_event_bus()
    token = _token(client, admin.email)

    with client.websocket_connect("/api/events") as socket:
        socket.send_text(json.dumps({"type": "auth", "token": token}))
        assert json.loads(socket.receive_text())["type"] == "ready"

        publish_capture_status("CAP-ANY", "ready", project_id="a-project-nobody-added-them-to")

        assert json.loads(socket.receive_text())["session_id"] == "CAP-ANY"
