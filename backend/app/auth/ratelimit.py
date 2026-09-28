"""Rate limiting for the unauthenticated endpoints (§44).

`/auth/login` is the only route anyone can reach without credentials that does *expensive* work.
Password verification is bcrypt, and bcrypt is slow on purpose — a few hundred milliseconds of
CPU per call is the entire point of it. That makes the login endpoint two problems at once:

* **Brute force.** Unlimited guesses against a known email address, at whatever rate the network
  allows.
* **Cost asymmetry, which is the sharper one.** A request costs the attacker nothing and costs
  this server a third of a second of CPU. A few hundred concurrent requests saturate the API
  without a single correct password — and the login endpoint is the one that has to keep working
  for anybody to use the product at all.

So there are two limits, and they exist for those two different reasons:

* **Per client address**, counting *every* attempt and checked before any hashing happens. This
  is the CPU control; it has to reject before doing the work, or it is not a control at all.
* **Per account**, counting only *failures*. This is the brute-force control. Someone signing in
  correctly forty times is not attacking anything, and counting their successes would lock out
  the busiest legitimate users first.

**The per-account limit can be used to lock someone out.** An attacker who knows an address can
spend failures against it and deny that person sign-in for the window. That is a real trade, not
an oversight: the alternative is leaving targeted brute force unbounded. It is mitigated by
keeping the window short and the lockout non-permanent, and by the per-address limit making the
attack cost something too. A deployment that would rather accept brute-force risk than lockout
risk can raise `AUTH_ACCOUNT_MAX_FAILURES`.

**It fails open.** If the Redis backing it is unreachable, requests are allowed rather than
refused. A limiter that fails closed turns a broker blip into a total outage of the one endpoint
everything else depends on — losing the protection is the smaller harm, and it is logged rather
than silent.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether to allow the attempt, and how long to wait if not."""

    allowed: bool
    retry_after: int = 0


class _Counter:
    """A fixed-window counter, in process memory.

    Fixed window rather than a sliding log: a sliding window needs a timestamp per attempt and
    this needs to be cheaper than the bcrypt call it is protecting. The known cost is burstiness
    at a boundary — up to twice the limit across two adjacent windows — which for a login control
    measured in tens of attempts is not worth a more complicated structure.

    **Per process.** Several uvicorn workers each keep their own counters, so the effective limit
    is the configured one multiplied by the worker count. That is why Redis is used when it is
    available, and why this is the fallback rather than the default.
    """

    def __init__(self) -> None:
        self._hits: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: int) -> Decision:
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            count, expires = self._hits.get(key, (0, now + window_seconds))
            if expires <= now:
                count, expires = 0, now + window_seconds
            count += 1
            self._hits[key] = (count, expires)
            if count > limit:
                return Decision(allowed=False, retry_after=int(expires - now) + 1)
        return Decision(allowed=True)

    def peek(self, key: str, limit: int) -> Decision:
        """Is this key already over its limit, without spending an attempt against it?

        Needed because the per-account check happens *before* the password is verified but is
        only incremented *after* a failure — checking and counting are separate events for that
        limit, unlike the per-address one.
        """
        now = time.monotonic()
        with self._lock:
            count, expires = self._hits.get(key, (0, 0.0))
            # `>=`, not `>`: this asks "has the allowance already been spent", where `hit` asks
            # "does spending one more exceed it". A limit of 3 means three failures are tolerated
            # and the fourth attempt is refused — using `>` here would quietly allow a fourth.
            if expires <= now or count < limit:
                return Decision(allowed=True)
            return Decision(allowed=False, retry_after=int(expires - now) + 1)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def _evict(self, now: float) -> None:
        """Drop expired keys, so an attacker cannot grow this dictionary without bound.

        Every distinct address and address-like string an attacker sends would otherwise stay
        resident for the life of the process — an unauthenticated memory leak, which is the same
        class of problem this module exists to prevent.
        """
        if len(self._hits) < 4096:
            return
        for key, (_, expires) in list(self._hits.items()):
            if expires <= now:
                del self._hits[key]


class _RedisCounter:
    """The same counter in Redis, so every API process shares one view.

    `INCR` then `EXPIRE` on first hit is the standard fixed window: atomic per key, one round
    trip in the common case, and self-cleaning because the key expires rather than being swept.
    """

    def __init__(self, url: str) -> None:
        from redis import Redis

        self._client = Redis.from_url(url)

    def hit(self, key: str, limit: int, window_seconds: int) -> Decision:
        try:
            count = int(self._client.incr(key))
            if count == 1:
                self._client.expire(key, window_seconds)
            if count > limit:
                return Decision(allowed=False, retry_after=self._ttl(key, window_seconds))
        except Exception:
            logger.warning("Rate limiter unavailable; allowing the request")
        return Decision(allowed=True)

    def peek(self, key: str, limit: int) -> Decision:
        try:
            raw = self._client.get(key)
            # `>=` for the same reason as the in-memory counter: the allowance is spent, not
            # exceeded.
            if raw is not None and int(raw) >= limit:
                return Decision(allowed=False, retry_after=self._ttl(key, 1))
        except Exception:
            logger.warning("Rate limiter unavailable; allowing the request")
        return Decision(allowed=True)

    def reset(self, key: str) -> None:
        try:
            self._client.delete(key)
        except Exception:
            logger.warning("Rate limiter unavailable; a counter was not cleared")

    def _ttl(self, key: str, fallback: int) -> int:
        try:
            ttl = int(self._client.ttl(key))
        except Exception:
            return fallback
        return ttl if ttl > 0 else fallback


_counter: _Counter | _RedisCounter | None = None


def get_counter() -> _Counter | _RedisCounter:
    """Redis when the deployment has it, process memory otherwise.

    Follows `queue_backend` for the same reason the event bus does: it is the existing signal
    for "this deployment has a Redis", and inventing a second setting invites the combination
    where one says yes and the other says no.
    """
    global _counter
    if _counter is None:
        if settings.queue_backend == "redis":
            try:
                _counter = _RedisCounter(settings.redis_url)
            except Exception:
                logger.warning("Rate limiting falls back to process memory; limits are per worker")
                _counter = _Counter()
        else:
            _counter = _Counter()
    return _counter


def reset_counter() -> None:
    """Drop the singleton. For tests."""
    global _counter
    _counter = None


def client_key(request: Request) -> str:
    """Who this request is from, for limiting purposes.

    Behind a proxy `request.client.host` is the proxy, so every user shares one bucket and the
    limit becomes a global one. The fix is a header — and a header is exactly what an attacker
    controls, so which one matters.

    `X-Real-IP` is used rather than the first entry of `X-Forwarded-For`. Our nginx sets
    `X-Real-IP` from `$remote_addr` with `proxy_set_header`, which *replaces* whatever the client
    sent. `X-Forwarded-For` is built with `$proxy_add_x_forwarded_for`, which **appends** to the
    client's value — so its leftmost entry is attacker-supplied, and limiting on it would let
    anyone bypass the limit entirely by rotating a fake header.

    Only trusted when `TRUST_PROXY_HEADERS` is on, because the header means nothing when the
    application is exposed directly: then anyone can set it.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-real-ip")
        if forwarded:
            return forwarded.strip()
    client = request.client
    return client.host if client else "unknown"


def _decide(operation: str, call: Callable[[], Decision]) -> Decision:
    """Run a limiter operation, allowing the request if the limiter itself fails.

    The backends catch their own broker errors, but this is the guarantee rather than the hope:
    any failure to *reach* a decision — a counter that could not be constructed, an error nobody
    anticipated — must not become a 500 on the endpoint everything else depends on. Failing open
    loses the protection; failing closed loses the product.
    """
    try:
        return call()
    except Exception:
        logger.warning("Rate limiter failed during %s; allowing the request", operation)
        return Decision(allowed=True)


def _record(operation: str, call: Callable[[], object]) -> None:
    """Same, for the operations whose result nobody reads."""
    try:
        call()
    except Exception:
        logger.warning("Rate limiter failed during %s", operation)


def _spend_address_budget(
    request: Request,
    *,
    namespace: str,
    limit: int,
    window_seconds: int,
    message: str,
    what: str,
) -> None:
    """Spend one attempt against the caller's address in one named bucket.

    ``namespace`` is not decoration. Two endpoints sharing a bucket share a fate: whichever is
    busier spends the budget and the other starts refusing, which is how a public route can lock
    people out of signing in. Every caller therefore names its own.
    """
    from app.core.errors import RateLimitedError

    if limit <= 0:
        return
    decision = _decide(
        f"{what} address check",
        lambda: get_counter().hit(
            f"ratelimit:{namespace}:addr:{client_key(request)}", limit, window_seconds
        ),
    )
    if not decision.allowed:
        logger.warning("Rate limited an address on a %s route", what)
        raise RateLimitedError(message, retry_after=decision.retry_after)


def check_address(request: Request) -> None:
    """Spend one attempt against the caller's address, before any expensive work.

    Raises :class:`~app.core.errors.RateLimitedError`. Counting happens here rather than after
    the outcome is known because the cost this limit protects — the password hash — is incurred
    regardless of whether the attempt succeeds.
    """
    _spend_address_budget(
        request,
        namespace="auth",
        limit=settings.auth_address_max_attempts,
        window_seconds=settings.auth_address_window_seconds,
        message="Too many sign-in attempts from this network. Wait a moment and try again.",
        what="auth",
    )


def check_share_address(request: Request) -> None:
    """The same idea for opening share links, in a bucket of its own.

    Deliberately **not** the auth bucket, which is what this used to spend. A share link is passed
    around a site team who are all behind one address, so a handful of people opening a link they
    were sent would quietly consume the sign-in allowance for everyone at that office — and be
    told, if they hit it, that there had been "too many sign-in attempts", which is nonsense to
    somebody who has no account and never tried to sign in.

    The limits are looser than auth's because what they protect is different. There is no bcrypt
    here and no secret to guess — the token is 256 bits — only an unauthenticated endpoint that
    signs URLs and writes a row on every request, which is worth a ceiling whether or not anyone
    is attacking it.
    """
    _spend_address_budget(
        request,
        namespace="share",
        limit=settings.share_address_max_views,
        window_seconds=settings.share_address_window_seconds,
        message="This link has been opened too many times just now. Wait a moment and retry.",
        what="share",
    )


def _account_key(email: str) -> str:
    return f"ratelimit:auth:acct:{email.strip().lower()}"


def check_account(email: str) -> None:
    """Refuse an account that has failed too many times recently."""
    from app.core.errors import RateLimitedError

    if settings.auth_account_max_failures <= 0:
        return
    decision = _decide(
        "account check",
        lambda: get_counter().peek(_account_key(email), settings.auth_account_max_failures),
    )
    if not decision.allowed:
        logger.warning("Rate limited an account after repeated failures")
        raise RateLimitedError(
            "Too many failed sign-in attempts for this account. "
            "Wait a few minutes before trying again.",
            retry_after=decision.retry_after,
        )


def record_failure(email: str) -> None:
    """Count one failed sign-in against the account."""
    if settings.auth_account_max_failures <= 0:
        return
    _record(
        "failure count",
        lambda: get_counter().hit(
            _account_key(email),
            settings.auth_account_max_failures,
            settings.auth_account_window_seconds,
        ),
    )


def clear_account(email: str) -> None:
    """Forget an account's failures after it signs in successfully.

    Without this a user who mistypes their password four times and then gets it right stays one
    mistake away from being locked out for the rest of the window, which is a bad experience
    built on no security benefit: the correct password has already been demonstrated.
    """
    if settings.auth_account_max_failures <= 0:
        return
    _record("account clear", lambda: get_counter().reset(_account_key(email)))
