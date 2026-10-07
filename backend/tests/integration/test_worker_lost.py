"""A capture must survive the worker that was processing it dying (§34).

When a worker is killed mid-stage its `ProcessingJob` row stays RUNNING and the capture stays
PROCESSING, and nothing ever reconsidered either. The result was a capture that could not be
recovered through the product at all:

* `POST /captures/{id}/retry` refused — the session is `processing`, not `failed`;
* `POST /captures/{id}/process` refused — a job is still active;
* the web client offered **no button**, because `processing` counts as in-flight, so "Retry"
  (which needs `failed`) and "Process" (which needs `!inFlight`) were both hidden.

The only way out was `POST /process` with `force=true`, by hand, which nothing in the interface
suggests. An operator's day of walking a site sat behind an animated spinner that would never
stop.

The fix is not a timeout. A stage commits RUNNING and then does not touch the row until it
finishes, so a long video extraction and a dead worker are indistinguishable by elapsed time —
guessing would fail live work and tell an operator to redo something that was working. The queue
is asked instead, and it answers "alive or cannot tell" unless it is certain the job is gone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.enums import CaptureSessionStatus, JobStatus, JobType
from app.models import CaptureSession, ProcessingJob
from tests.conftest import auth_headers


@pytest.fixture
def capture(client: TestClient, admin, db, panorama_factory):
    """An uploaded capture, and the headers to reach it.

    With a real file in it, because an empty capture cannot be processed at all — a retry would
    be refused for that reason instead of the one under test, and the test would pass for the
    wrong reason.
    """
    headers = auth_headers(client, admin.email)
    project = client.post(
        "/api/projects", json={"name": "Lost", "code": "LOST-1"}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "B"}, headers=headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "L1", "level": 1},
        headers=headers,
    ).json()
    session_id = client.post(
        "/api/captures", json={"floor_id": floor["id"]}, headers=headers
    ).json()["id"]
    uploaded = client.post(
        f"/api/captures/{session_id}/upload",
        files={"files": ("one.jpg", panorama_factory((0.0, 0.0, 0.0)), "image/jpeg")},
        headers=headers,
    )
    assert uploaded.status_code in {200, 201}, uploaded.text
    return headers, session_id


def strand(db, session_id: str, *, started_ago: timedelta) -> str:
    """Leave behind exactly what a killed worker leaves: a RUNNING row and a PROCESSING capture."""
    session = db.get(CaptureSession, session_id)
    session.status = CaptureSessionStatus.PROCESSING
    job = ProcessingJob(
        session_id=session_id,
        job_type=JobType.MAPPING,
        status=JobStatus.RUNNING,
        attempt=1,
        progress=0.4,
        queue_job_id="thread-deadbeef0001",
        started_at=datetime.now(UTC) - started_ago,
    )
    db.add(job)
    db.commit()
    return str(job.id)


def test_a_capture_whose_worker_died_can_be_retried(client: TestClient, capture, db) -> None:
    """The whole point: reading the status is what un-sticks it.

    `started_ago` is an hour, which with the in-process queue means the job began before this
    queue existed — and work in a thread pool cannot outlive the process holding it. That is a
    fact, not a timeout.
    """
    headers, session_id = capture
    strand(db, session_id, started_ago=timedelta(hours=1))

    status = client.get(f"/api/captures/{session_id}/status", headers=headers)

    assert status.status_code == 200, status.text
    body = status.json()
    assert body["status"] == CaptureSessionStatus.FAILED
    assert "worker" in (body["failure_reason"] or "").lower()
    stage = next(row for row in body["stages"] if row["job_type"] == "mapping")
    assert stage["status"] == "failed"
    assert stage["error_code"] == "worker_lost"

    # And now the ordinary recovery path works, which is the part that was missing.
    retried = client.post(f"/api/captures/{session_id}/retry", headers=headers)
    assert retried.status_code == 200, retried.text


def test_a_stage_that_is_still_running_is_left_alone(client: TestClient, capture, db) -> None:
    """The failure this must never cause: killing live work.

    A job started *after* the current queue came up may genuinely be running in it, however long
    it has been going — a long video extraction looks exactly like this. The queue says "alive or
    cannot tell", and that is treated as alive.
    """
    headers, session_id = capture
    strand(db, session_id, started_ago=timedelta(seconds=0))

    body = client.get(f"/api/captures/{session_id}/status", headers=headers).json()

    assert body["status"] == CaptureSessionStatus.PROCESSING
    stage = next(row for row in body["stages"] if row["job_type"] == "mapping")
    assert stage["status"] == "running"


def test_a_job_with_no_recorded_queue_id_is_left_alone(client: TestClient, capture, db) -> None:
    """Unknown is not dead. A row with no queue id answers nothing either way."""
    headers, session_id = capture
    session = db.get(CaptureSession, session_id)
    session.status = CaptureSessionStatus.PROCESSING
    job = ProcessingJob(
        session_id=session_id,
        job_type=JobType.QUALITY,
        status=JobStatus.RUNNING,
        attempt=1,
        queue_job_id=None,
        started_at=None,
    )
    db.add(job)
    db.commit()

    body = client.get(f"/api/captures/{session_id}/status", headers=headers).json()

    assert body["status"] == CaptureSessionStatus.PROCESSING
    stage = next(row for row in body["stages"] if row["job_type"] == "quality")
    assert stage["status"] == "running"


def test_a_completed_capture_is_not_disturbed(client: TestClient, capture, db) -> None:
    """A finished capture has no RUNNING rows, so the sweep must do nothing at all."""
    headers, session_id = capture
    session = db.get(CaptureSession, session_id)
    session.status = CaptureSessionStatus.READY
    db.add(
        ProcessingJob(
            session_id=session_id,
            job_type=JobType.MAPPING,
            status=JobStatus.COMPLETE,
            attempt=1,
            progress=1.0,
            started_at=datetime.now(UTC) - timedelta(days=2),
        )
    )
    db.commit()

    body = client.get(f"/api/captures/{session_id}/status", headers=headers).json()

    assert body["status"] == CaptureSessionStatus.READY
    assert body["failure_reason"] is None


def test_the_thread_queue_calls_its_own_leftovers_abandoned() -> None:
    """The unit of the rule, stated against the backend rather than through HTTP."""
    from app.queue.thread import ThreadQueue

    queue = ThreadQueue(workers=1)
    try:
        before = datetime.now(UTC) - timedelta(minutes=5)
        after = datetime.now(UTC) + timedelta(minutes=5)

        assert queue.is_abandoned("thread-x", before) is True
        assert queue.is_abandoned("thread-x", after) is False
        # No timestamp is no evidence.
        assert queue.is_abandoned("thread-x", None) is False
    finally:
        queue.close()
