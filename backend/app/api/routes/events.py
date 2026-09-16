"""The WebSocket clients listen on (§26, §34).

**Why authentication is a message and not a query parameter.** A browser cannot set headers on
`new WebSocket(url)` — there is no equivalent of `Authorization` — so the token has to arrive
some other way. The usual shortcut is `?token=…`, and it puts a bearer credential into every
access log, every proxy log and the browser's own history, where it stays valid until it expires.
So the socket opens unauthenticated, carries nothing, and the first thing the client must send is
its token. Until that arrives the connection is subscribed to nothing.

**Why the socket does not carry data the client could not otherwise fetch.** Events are hints —
"this capture moved, ask again" — not resources. That keeps one authorisation check (the token,
at connect) sufficient: a client cannot learn anything from an event that `GET /captures/{id}`
would refuse it. It also makes a dropped message harmless, which is the property the whole design
rests on, because the client polls as a floor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.auth.security import decode_access_token
from app.core.logging import get_logger
from app.database.session import SessionLocal
from app.events import get_event_bus
from app.models import User

router = APIRouter(tags=["events"])
logger = get_logger(__name__)

#: How long a client has to send its token before the socket is closed.
#:
#: Short, because an unauthenticated socket is an open file descriptor that anyone can create.
#: Long enough that a phone on a slow link can still get one small frame through.
AUTH_TIMEOUT_SECONDS = 10.0

#: A policy-violation close. Distinct from a network failure, so the client can tell "my token is
#: no good, stop retrying" from "the connection dropped, try again".
CLOSE_POLICY_VIOLATION = 1008


def _authenticate(token: str) -> User | None:
    """The same checks `get_current_user` makes, against a token from a socket frame.

    Deliberately re-stated rather than shared: the HTTP dependency raises to produce a 401
    response, which is not a thing a WebSocket can send once it is open. Both must stay in step —
    in particular the role check, without which a demoted user keeps their old permissions on an
    open socket until the token expires.
    """
    try:
        claims = decode_access_token(token)
    except Exception:
        return None

    db = SessionLocal()
    try:
        user = db.get(User, claims.subject)
        if user is None or not user.is_active or user.role != claims.role:
            return None
        return user
    finally:
        db.close()


async def _await_token(websocket: WebSocket) -> User | None:
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
    except (TimeoutError, WebSocketDisconnect):
        return None

    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(message, dict) or message.get("type") != "auth":
        return None
    token = message.get("token")
    return _authenticate(token) if isinstance(token, str) and token else None


@router.websocket("/events")
async def capture_events(websocket: WebSocket) -> None:
    """Push capture-processing changes, so the client stops asking every twenty seconds.

    The sequence is fixed and the client's side of it is in `frontend/src/lib/events.ts`:

    1. the socket opens;
    2. the client sends ``{"type": "auth", "token": "<jwt>"}``;
    3. the server replies ``{"type": "ready"}`` and events follow.

    Nothing is subscribed before step 3. A client that skips step 2 is closed on the timeout
    rather than left connected and silent, which would be indistinguishable from a deployment
    where the pipeline is simply idle.
    """
    await websocket.accept()

    user = await _await_token(websocket)
    if user is None:
        with contextlib.suppress(Exception):
            await websocket.close(code=CLOSE_POLICY_VIOLATION, reason="Authentication required")
        return

    await websocket.send_text(json.dumps({"type": "ready"}))
    logger.info("Event socket open", extra={"user_id": user.id})

    # Subscribing only now is what makes the auth step meaningful; a subscription opened before
    # the token arrived would have to be discarded, and forgetting to discard it is the whole
    # class of bug this ordering removes.
    events = await get_event_bus().subscribe()
    try:
        async for event in events:
            if websocket.client_state is not WebSocketState.CONNECTED:
                break
            await websocket.send_text(event.to_json())
    except WebSocketDisconnect:
        pass
    except Exception:
        # A send that fails means the peer is gone, which is ordinary. Logged rather than raised
        # so it does not surface as a 500 in the API's error handler for a connection that has
        # already closed.
        logger.info("Event socket closed unexpectedly", extra={"user_id": user.id})
    finally:
        # Closing the generator is what removes the subscriber from the bus. Without it a
        # long-running process accumulates queues nobody reads, one per connection ever made.
        with contextlib.suppress(Exception):
            await events.aclose()
        with contextlib.suppress(Exception):
            if websocket.client_state is WebSocketState.CONNECTED:
                await websocket.close()
