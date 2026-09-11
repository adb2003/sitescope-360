"""Tests for the worker container healthcheck.

Redis is not installed on the development machine, so the interesting branches of
:mod:`app.workers.healthcheck` -- worker registered, worker missing, heartbeat stale -- cannot be
reached by running it. They are reached here with fakes instead.

That is deliberate rather than convenient. The bug this module fixes was a healthcheck that could
never pass, and it survived because nothing local exercised it; shipping its replacement with the
same blind spot would repeat the mistake. What these tests cannot prove is that RQ registers a
worker under ``socket.gethostname()`` -- only the real stack shows that, and the CI smoke test
now runs the healthcheck inside the worker container for exactly that reason.
"""

from __future__ import annotations

import datetime
import socket

import pytest
import redis
import rq

from app.core.config import settings
from app.workers import healthcheck


class _FakeRedis:
    def __init__(self, *, reachable: bool = True) -> None:
        self._reachable = reachable

    def ping(self) -> bool:
        if not self._reachable:
            raise ConnectionError("connection refused")
        return True


class _FakeWorker:
    def __init__(self, hostname: str | None, heartbeat_age_seconds: float | None) -> None:
        self.hostname = hostname
        if heartbeat_age_seconds is None:
            self.last_heartbeat: datetime.datetime | None = None
        else:
            self.last_heartbeat = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
                seconds=heartbeat_age_seconds
            )


@pytest.fixture
def redis_backend(monkeypatch: pytest.MonkeyPatch):
    """Point the healthcheck at a fake Redis and a fake worker registry."""
    monkeypatch.setattr(settings, "queue_backend", "redis", raising=False)
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *a, **k: _FakeRedis()))
    monkeypatch.setattr(rq, "Queue", lambda *a, **k: object())

    def _register(workers: list[_FakeWorker]) -> None:
        monkeypatch.setattr(rq.Worker, "all", staticmethod(lambda *a, **k: workers))

    _register([])
    return _register


def test_thread_backend_needs_no_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API runs the pool in-process, so reporting unhealthy would be a false alarm."""
    monkeypatch.setattr(settings, "queue_backend", "thread", raising=False)
    assert healthcheck.main() == 0


def test_unreachable_redis_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "queue_backend", "redis", raising=False)
    monkeypatch.setattr(
        redis.Redis, "from_url", staticmethod(lambda *a, **k: _FakeRedis(reachable=False))
    )
    assert healthcheck.main() == 1


def test_a_live_worker_on_this_host_is_healthy(redis_backend) -> None:
    redis_backend([_FakeWorker(socket.gethostname(), heartbeat_age_seconds=5.0)])
    assert healthcheck.main() == 0


def test_no_registered_worker_is_unhealthy(redis_backend) -> None:
    """The original failure mode: the process may be up while no worker exists."""
    redis_backend([])
    assert healthcheck.main() == 1


def test_another_replicas_worker_does_not_count(redis_backend) -> None:
    """The check must be about *this* container.

    Accepting any worker on the queue would report every replica healthy while this one is dead,
    which is the failure the healthcheck exists to catch.
    """
    redis_backend([_FakeWorker("some-other-container", heartbeat_age_seconds=1.0)])
    assert healthcheck.main() == 1


def test_a_stale_heartbeat_is_unhealthy(redis_backend) -> None:
    """A registered worker is not a working worker: RQ's key outlives the process."""
    age = healthcheck.MAX_HEARTBEAT_AGE_SECONDS + 60.0
    redis_backend([_FakeWorker(socket.gethostname(), heartbeat_age_seconds=age)])
    assert healthcheck.main() == 1


def test_a_worker_that_never_beat_is_unhealthy(redis_backend) -> None:
    redis_backend([_FakeWorker(socket.gethostname(), heartbeat_age_seconds=None)])
    assert healthcheck.main() == 1


def test_an_unreadable_registry_is_unhealthy(
    redis_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry that raises must not be mistaken for an empty one, nor crash the probe."""

    def _explode(*args, **kwargs):
        raise RuntimeError("redis protocol error")

    monkeypatch.setattr(rq.Worker, "all", staticmethod(_explode))
    assert healthcheck.main() == 1
