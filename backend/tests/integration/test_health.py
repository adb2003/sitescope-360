"""What `GET /api/system/health` is willing to call healthy.

The endpoint probed the database and asked the queue how it was, and for storage it reported
`settings.storage_backend` — the *name* of the configured backend. That value is "local" or "s3"
whether or not the bucket exists, the credentials work, or the disk has any room left.

A capture platform whose object store is unreachable cannot serve one image or accept one upload.
Answering "ok" through that is not a missing feature; it is the monitoring actively suppressing
the alarm, and a load balancer keeping traffic on an instance that can do nothing useful.

So the test that matters here is the failing one. The happy path is easy to get right by
accident — `"ok"` is the default answer of anything that does not look.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_reports_storage_as_a_probe_not_a_setting(client: TestClient) -> None:
    """The name of a backend is not evidence that it works."""
    body = client.get("/api/system/health").json()

    storage = body["storage"]
    assert isinstance(storage, dict), f"storage should be a probe result, got {storage!r}"
    assert storage["ok"] is True
    assert storage["backend"]
    assert body["status"] == "ok"


def test_unreachable_storage_makes_the_service_degraded(client: TestClient, monkeypatch) -> None:
    """The case the change exists for.

    Before this, an object store that had gone away left health answering "ok" — so nothing
    paged, the load balancer kept the instance in rotation, and every request for an image
    failed behind a green check.
    """
    from app.api.routes import admin

    class Broken:
        def health(self) -> dict[str, object]:
            return {"backend": "s3", "ok": False, "error": "connection refused", "bucket": "b"}

    monkeypatch.setattr(admin, "get_storage", lambda: Broken())

    body = client.get("/api/system/health").json()

    assert body["status"] == "degraded"
    assert body["storage"]["ok"] is False
    assert "connection refused" in body["storage"]["error"]
    # The database is fine, and saying so is the point: an operator needs to know which half to
    # go and look at, which a bare "degraded" would not tell them.
    assert body["database"]["ok"] is True


def test_a_probe_that_raises_never_takes_the_endpoint_down(client: TestClient, monkeypatch) -> None:
    """A liveness probe that 500s reports the wrong failure.

    It tells a load balancer the application is broken when the truth is narrower — and the
    providers are written to return rather than raise, so anything arriving here is already a
    surprise. Surprises in a health check must still produce an answer.
    """
    from app.api.routes import admin

    class Exploding:
        def health(self) -> dict[str, object]:
            raise RuntimeError("boto3 blew up")

    monkeypatch.setattr(admin, "get_storage", lambda: Exploding())

    response = client.get("/api/system/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "degraded"
    assert "boto3 blew up" in body["storage"]["error"]


def test_the_local_backend_probes_by_writing(client: TestClient, storage_root) -> None:
    """A read would pass on a filesystem gone read-only or full — the two ways this fails.

    So the probe writes and cleans up after itself. Asserting the cleanup matters: a health check
    that leaves a file behind every time something polls it is a slow leak in the directory it
    is supposed to be watching.
    """
    from app.storage import get_storage

    result = get_storage().health()

    assert result["ok"] is True
    assert not (storage_root / ".health").exists(), "the probe must remove what it wrote"


def test_the_events_endpoint_reports_what_the_platform_actually_logs(
    client: TestClient, admin_headers
) -> None:
    """The endpoint's whole job is to describe the system, and it was misdescribing it.

    `logged_event_names` was a hard-coded list of eight while the code emitted thirty-five. A
    reader with no log aggregator attached has nothing else to go on, so the number being wrong
    was not a documentation lapse but a false statement served from the API.

    `tests/unit/test_audit_events.py` holds the registry to the code; this holds the *endpoint*
    to the registry, which is the other half and the one a refactor is likelier to break.
    """
    from app.core.audit import EVENT_NAMES

    response = client.get("/api/system/events", headers=admin_headers)

    assert response.status_code == 200, response.text
    reported = response.json()["logged_event_names"]
    assert reported == list(EVENT_NAMES)
    assert len(reported) > 40, "the registry is meant to be the whole set, not a sample"
    for destructive in ("capture.deleted", "auth.role_changed", "location.deleted"):
        assert destructive in reported
