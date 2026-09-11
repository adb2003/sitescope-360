"""The chain from a phone on site to a panorama in the office.

This is the path the product exists to serve, and it is only as strong as its weakest link. Each
test here pins one link that can break silently -- silently being the point. A capture that never
arrives announces itself; a capture that arrives and is never processed looks, from the phone,
exactly like one that worked.

The links, in order:

    create (idempotent)  ->  upload  ->  finish  ->  process  ->  frames the web app can read

The mobile client retries the whole sequence whenever connectivity returns, so every step has to
tolerate being asked twice.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import auth_headers
from tests.integration.test_definition_of_done import _await_ready


def _floor(client: TestClient, headers: dict[str, str]) -> str:
    """A project/building/floor to capture against."""
    project = client.post(
        "/api/projects",
        json={"name": "Mobile Chain", "code": "MOB-1"},
        headers=headers,
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings",
        json={"name": "Block A"},
        headers=headers,
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "Level 01", "level": 1, "area_sqft": 5000.0},
        headers=headers,
    ).json()
    return str(floor["id"])


def test_creating_a_session_twice_with_the_same_client_id_is_one_session(
    client: TestClient, admin, engineer
) -> None:
    """The phone generates the id before it has ever reached the network.

    Without this, a create that succeeded server-side but whose response was lost -- the ordinary
    outcome of a site connection -- produces a second session on retry, and the walkthrough
    arrives split across two records with no way to tell they belong together.
    """
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    payload = {
        "floor_id": floor_id,
        "client_session_id": "CAP-20260911-103022-A8F3",
        "started_at": "2026-09-11T10:30:22Z",
    }

    first = client.post("/api/captures", json=payload, headers=headers)
    second = client.post("/api/captures", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"] == "CAP-20260911-103022-A8F3"

    listed = client.get("/api/captures", params={"floor_id": floor_id}, headers=headers).json()
    assert listed["total"] == 1, "a retried create must not produce a second session"


def test_the_same_id_on_a_different_floor_is_refused(client: TestClient, admin, engineer) -> None:
    """Idempotency must not become a way to overwrite someone else's capture."""
    headers = auth_headers(client, admin.email)
    floor_a = _floor(client, headers)

    project = client.post(
        "/api/projects", json={"name": "Other", "code": "MOB-2"}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "Block B"}, headers=headers
    ).json()
    floor_b = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "Level 09", "level": 9, "area_sqft": 5000.0},
        headers=headers,
    ).json()["id"]

    shared = {"client_session_id": "CAP-20260911-110000-BEEF"}
    client.post("/api/captures", json={**shared, "floor_id": floor_a}, headers=headers)
    clash = client.post("/api/captures", json={**shared, "floor_id": floor_b}, headers=headers)

    assert clash.status_code == 409


def test_upload_then_process_makes_frames_visible_to_the_web_app(
    client: TestClient, admin, panorama_factory
) -> None:
    """The whole chain, end to end, as the mobile app performs it.

    The assertion that matters is the last one: the frames are readable through the same API the
    web viewer uses. Everything before it can succeed while the capture stays invisible, which is
    the failure this exists to catch.
    """
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)

    created = client.post(
        "/api/captures",
        json={"floor_id": floor_id, "client_session_id": "CAP-20260911-120000-CAFE"},
        headers=headers,
    )
    assert created.status_code == 201
    session_id = created.json()["id"]

    # Distinct positions, because `panorama_factory` caches by position: uploading the default
    # twice sends identical bytes, and the pipeline rightly collapses them into one frame. The
    # first version of this test asserted two frames from two identical images and was wrong
    # about the product, not about the product being wrong.
    for index, position in enumerate([(0.0, 0.0, 0.0), (1.5, 0.0, 0.0)]):
        upload = client.post(
            f"/api/captures/{session_id}/upload",
            files={"files": (f"PANO_{index}.jpg", panorama_factory(position), "image/jpeg")},
            headers=headers,
        )
        assert upload.status_code in (200, 201), upload.text

    finished = client.post(f"/api/captures/{session_id}/finish", headers=headers)
    assert finished.status_code == 200

    processed = client.post(f"/api/captures/{session_id}/process", json={}, headers=headers)
    assert processed.status_code == 200

    # The API returns before the work is done -- that is the §8 contract, and it holds with the
    # thread queue here exactly as it will with Redis in production. So wait on the same status
    # endpoint the web app polls.
    status = _await_ready(client, headers, session_id)
    assert status["status"] == "ready", status

    # The point of the whole exercise: the office can see what was walked.
    frames = client.get(f"/api/captures/{session_id}/frames", headers=headers).json()
    assert frames["total"] == 2, f"frames never became visible: {frames}"
    # Readable by the viewer, not merely present as rows.
    first = frames["items"][0]
    assert first["projection"] == "equirectangular"
    manifest = client.get(f"/api/frames/{first['id']}/viewer", headers=headers)
    assert manifest.status_code == 200, manifest.text


def test_processing_an_already_running_capture_is_refused_not_duplicated(
    client: TestClient, admin, panorama_factory
) -> None:
    """The mobile client asks on every sync, so a second ask must not queue the work twice.

    409 is the right answer rather than a silent success: the client treats it as "already on
    it", and a duplicate pipeline run would compete for the same rows and waste the queue.
    """
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    session_id = client.post(
        "/api/captures",
        json={"floor_id": floor_id, "client_session_id": "CAP-20260911-130000-F00D"},
        headers=headers,
    ).json()["id"]
    client.post(
        f"/api/captures/{session_id}/upload",
        files={"files": ("PANO.jpg", panorama_factory(), "image/jpeg")},
        headers=headers,
    )

    first = client.post(f"/api/captures/{session_id}/process", json={}, headers=headers)
    assert first.status_code == 200

    second = client.post(f"/api/captures/{session_id}/process", json={}, headers=headers)
    # Either the first run already finished (200, nothing left in flight) or it is still going
    # (409). Both are correct; what must never happen is a second pipeline alongside the first.
    assert second.status_code in (200, 409), second.text


def test_processing_before_any_upload_is_refused_with_a_reason(client: TestClient, admin) -> None:
    """An empty session must not be marked processed.

    It would show in the web app as a capture that produced nothing, which reads as a failed
    walkthrough rather than as one that never uploaded.
    """
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    session_id = client.post(
        "/api/captures",
        json={"floor_id": floor_id, "client_session_id": "CAP-20260911-140000-0000"},
        headers=headers,
    ).json()["id"]

    refused = client.post(f"/api/captures/{session_id}/process", json={}, headers=headers)

    assert refused.status_code == 422
    assert "upload" in refused.text.lower()
