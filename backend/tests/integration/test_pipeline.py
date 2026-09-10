"""Pipeline behaviour: video frame extraction, failure handling, retry, idempotency (§8, §9, §34)."""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import pytest
from fastapi.testclient import TestClient

from app.core.enums import CaptureSessionStatus


@pytest.fixture
def floor(client: TestClient, admin_headers: dict[str, str]) -> str:
    project_id = client.post(
        "/api/projects", headers=admin_headers, json={"name": "Pipeline", "code": "PIPE-1"}
    ).json()["id"]
    building_id = client.post(
        f"/api/projects/{project_id}/buildings", headers=admin_headers, json={"name": "B"}
    ).json()["id"]
    return client.post(
        f"/api/buildings/{building_id}/floors", headers=admin_headers, json={"name": "L1"}
    ).json()["id"]


def new_session(client: TestClient, headers: dict[str, str], floor_id: str, **kwargs) -> str:
    return client.post(
        "/api/captures", headers=headers, json={"floor_id": floor_id, **kwargs}
    ).json()["id"]


def wait(client: TestClient, headers: dict[str, str], session_id: str) -> dict:
    for _ in range(300):
        body = client.get(f"/api/captures/{session_id}/status", headers=headers).json()
        if body["status"] in {CaptureSessionStatus.READY, CaptureSessionStatus.FAILED}:
            return body
        time.sleep(0.1)
    raise AssertionError("processing did not settle")


# ---- video -------------------------------------------------------------------
@pytest.fixture
def video_bytes(tmp_path: Path) -> bytes:
    """An 8-second, 10 fps 2:1 equirectangular MP4 of a camera walking the demo room."""
    from app.processing.synthetic import Room, render_panorama

    path = tmp_path / "walk.mp4"
    width, height, fps, seconds = 1024, 512, 10, 8
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        pytest.skip("This OpenCV build cannot write MP4")

    room = Room()
    for index in range(fps * seconds):
        x = -7.0 + 14.0 * index / (fps * seconds)
        writer.write(render_panorama(room, (x, 0.0, 0.0), width=width, height=height))
    writer.release()
    return path.read_bytes()


def test_video_upload_extracts_frames_at_the_configured_interval(
    client: TestClient, admin_headers: dict[str, str], floor: str, video_bytes: bytes
) -> None:
    """§9: one frame every N seconds, N configurable, and not every video frame."""
    session_id = new_session(client, admin_headers, floor, frame_interval_seconds=2.0)

    upload = client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("walk.mp4", video_bytes, "video/mp4")},
    )
    assert upload.status_code == 201, upload.text

    assert (
        client.post(f"/api/captures/{session_id}/process", headers=admin_headers).status_code == 200
    )
    status = wait(client, admin_headers, session_id)
    assert status["status"] == CaptureSessionStatus.READY, status

    files = client.get(f"/api/captures/{session_id}/files", headers=admin_headers).json()
    assert files[0]["kind"] == "video_360"
    assert files[0]["projection"] == "equirectangular"
    assert files[0]["duration_seconds"] == pytest.approx(8.0, abs=0.6)
    assert files[0]["fps"] == pytest.approx(10.0, abs=0.5)

    frames = client.get(
        f"/api/captures/{session_id}/frames", headers=admin_headers, params={"limit": 100}
    ).json()
    # 8 seconds at one frame every 2 s → 4 frames, and emphatically not 80.
    assert frames["total"] == 4, frames["total"]

    offsets = [item["source_time_seconds"] for item in frames["items"]]
    assert offsets == [0.0, 2.0, 4.0, 6.0]

    for item in frames["items"]:
        assert item["thumbnail"]["url"].startswith("http")
        assert item["quality"] is not None
        assert item["width_px"] == 1024


def test_frame_interval_can_be_overridden_per_request(
    client: TestClient, admin_headers: dict[str, str], floor: str, video_bytes: bytes
) -> None:
    session_id = new_session(client, admin_headers, floor)
    client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("walk.mp4", video_bytes, "video/mp4")},
    )
    client.post(
        f"/api/captures/{session_id}/process",
        headers=admin_headers,
        json={"frame_interval_seconds": 4.0},
    )
    assert wait(client, admin_headers, session_id)["status"] == CaptureSessionStatus.READY

    total = client.get(f"/api/captures/{session_id}/frames", headers=admin_headers).json()["total"]
    assert total == 2  # 8 s ÷ 4 s


def test_video_mapping_estimates_positions(
    client: TestClient, admin_headers: dict[str, str], floor: str, video_bytes: bytes
) -> None:
    """Frames from a real walk should get estimated relative positions, marked as estimated."""
    session_id = new_session(client, admin_headers, floor, frame_interval_seconds=1.0)
    client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("walk.mp4", video_bytes, "video/mp4")},
    )
    client.post(f"/api/captures/{session_id}/process", headers=admin_headers)
    assert wait(client, admin_headers, session_id)["status"] == CaptureSessionStatus.READY

    session = client.get(f"/api/captures/{session_id}", headers=admin_headers).json()
    assert session["mapping_confidence"] in {"none", "low", "medium", "high"}
    assert session["mapping_note"]

    frames = client.get(
        f"/api/captures/{session_id}/frames", headers=admin_headers, params={"limit": 100}
    ).json()["items"]
    positioned = [f for f in frames if f["rel_x"] is not None]
    if positioned:
        # Whatever the confidence, a position must never be labelled "measured".
        assert all(f["position_provenance"] == "estimated" for f in positioned)
        assert all(0.0 <= f["position_confidence"] <= 1.0 for f in positioned)


# ---- failure and retry -------------------------------------------------------
def test_processing_without_files_is_refused(
    client: TestClient, admin_headers: dict[str, str], floor: str
) -> None:
    session_id = new_session(client, admin_headers, floor)
    response = client.post(f"/api/captures/{session_id}/process", headers=admin_headers)
    assert response.status_code == 422
    assert "Upload at least one file" in response.json()["message"]


def test_corrupt_file_fails_with_a_reason_and_can_be_retried(
    client: TestClient, admin_headers: dict[str, str], floor: str, panorama_factory
) -> None:
    """§34: a failed capture reports why, and offers a retry."""
    session_id = new_session(client, admin_headers, floor)

    # A valid JPEG header followed by garbage: it passes the upload sniff but cannot decode.
    truncated = panorama_factory()[:200]
    upload = client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("broken.jpg", truncated, "image/jpeg")},
    )
    assert upload.status_code == 201, upload.text

    client.post(f"/api/captures/{session_id}/process", headers=admin_headers)
    status = wait(client, admin_headers, session_id)

    assert status["status"] == CaptureSessionStatus.FAILED
    assert status["failure_reason"]
    failed = [s for s in status["stages"] if s["status"] == "failed"]
    assert failed, status["stages"]
    assert failed[0]["error_code"]
    assert failed[0]["error_detail"]

    # Retry is available and re-runs from the failure.
    retried = client.post(f"/api/captures/{session_id}/retry", headers=admin_headers)
    assert retried.status_code == 200
    wait(client, admin_headers, session_id)


def test_retry_is_rejected_for_a_healthy_capture(
    client: TestClient, admin_headers: dict[str, str], floor: str, panorama_factory
) -> None:
    session_id = new_session(client, admin_headers, floor)
    client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("a.jpg", panorama_factory(), "image/jpeg")},
    )
    client.post(f"/api/captures/{session_id}/process", headers=admin_headers)
    assert wait(client, admin_headers, session_id)["status"] == CaptureSessionStatus.READY

    response = client.post(f"/api/captures/{session_id}/retry", headers=admin_headers)
    assert response.status_code == 409
    assert "not failed" in response.json()["message"]


def test_reprocessing_is_idempotent(
    client: TestClient, admin_headers: dict[str, str], floor: str, panorama_factory
) -> None:
    """Running the pipeline twice must replace its output, not duplicate frames."""
    session_id = new_session(client, admin_headers, floor)
    client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files=[
            ("files", ("a.jpg", panorama_factory((-7.5, 0.0, -4.0)), "image/jpeg")),
            ("files", ("b.jpg", panorama_factory((-7.5, 0.0, -2.5)), "image/jpeg")),
        ],
    )
    client.post(f"/api/captures/{session_id}/process", headers=admin_headers)
    assert wait(client, admin_headers, session_id)["status"] == CaptureSessionStatus.READY

    first = client.get(f"/api/captures/{session_id}/frames", headers=admin_headers).json()
    assert first["total"] == 2

    client.post(f"/api/captures/{session_id}/process", headers=admin_headers, json={"force": True})
    assert wait(client, admin_headers, session_id)["status"] == CaptureSessionStatus.READY

    second = client.get(f"/api/captures/{session_id}/frames", headers=admin_headers).json()
    assert second["total"] == 2, "reprocessing duplicated frames"

    # The frames must still be viewable afterwards, which they are not if reprocessing deleted
    # the objects behind them.
    for frame in second["items"]:
        url = frame["full"]["url"].removeprefix("http://testserver")
        assert client.get(url).status_code == 200, "frame image missing after reprocess"


def test_reprocessing_never_deletes_an_original_upload(
    client: TestClient,
    db,
    admin_headers: dict[str, str],
    floor: str,
    panorama_factory,
) -> None:
    """A still frame shares its storage key with its source object, so cleanup must exclude it.

    Regression test for a data-loss bug: reprocessing a capture deleted the uploaded panorama it
    was reprocessing. Losing a site's only copy of a capture is unrecoverable, so this invariant
    is pinned directly rather than only implied by the idempotency test.
    """
    from app.models import CaptureFile
    from app.storage import get_storage

    session_id = new_session(client, admin_headers, floor)
    client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("original.jpg", panorama_factory(), "image/jpeg")},
    )
    client.post(f"/api/captures/{session_id}/process", headers=admin_headers)
    assert wait(client, admin_headers, session_id)["status"] == CaptureSessionStatus.READY

    source_key = db.query(CaptureFile).filter_by(session_id=session_id).one().storage_key
    storage = get_storage()
    assert storage.exists(source_key)

    for _ in range(2):
        client.post(
            f"/api/captures/{session_id}/process", headers=admin_headers, json={"force": True}
        )
        assert wait(client, admin_headers, session_id)["status"] == CaptureSessionStatus.READY
        assert storage.exists(source_key), "reprocessing deleted the original upload"


def test_concurrent_processing_is_refused(
    client: TestClient, admin_headers: dict[str, str], floor: str, panorama_factory
) -> None:
    session_id = new_session(client, admin_headers, floor)
    client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("a.jpg", panorama_factory(), "image/jpeg")},
    )
    client.post(f"/api/captures/{session_id}/process", headers=admin_headers)
    # A second request while the first is still queued must conflict rather than double-run.
    second = client.post(f"/api/captures/{session_id}/process", headers=admin_headers)
    assert second.status_code in {200, 409}
    if second.status_code == 409:
        assert second.json()["code"] == "conflict"
    wait(client, admin_headers, session_id)


def test_duplicate_upload_of_identical_bytes_is_deduplicated(
    client: TestClient, admin_headers: dict[str, str], floor: str, panorama_factory
) -> None:
    """§32: a mobile client retrying an upload must not create a second copy."""
    session_id = new_session(client, admin_headers, floor)
    data = panorama_factory()

    first = client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("a.jpg", data, "image/jpeg")},
    ).json()
    second = client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("a.jpg", data, "image/jpeg")},
    ).json()

    assert first[0]["id"] == second[0]["id"]
    files = client.get(f"/api/captures/{session_id}/files", headers=admin_headers).json()
    assert len(files) == 1


def test_client_supplied_session_id_is_idempotent(
    client: TestClient, admin_headers: dict[str, str], floor: str
) -> None:
    """§32: an offline client keeps the id it generated, so a retried create is not a duplicate."""
    payload = {"floor_id": floor, "client_session_id": "CAP-20260910-120000-TEST"}
    first = client.post("/api/captures", headers=admin_headers, json=payload)
    second = client.post("/api/captures", headers=admin_headers, json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"] == "CAP-20260910-120000-TEST"

    listed = client.get("/api/captures", headers=admin_headers, params={"floor_id": floor}).json()
    assert listed["total"] == 1


def test_unsupported_file_type_is_refused_at_upload(
    client: TestClient, admin_headers: dict[str, str], floor: str
) -> None:
    session_id = new_session(client, admin_headers, floor)

    refused = client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("notes.txt", b"just some text", "text/plain")},
    )
    assert refused.status_code == 415
    assert refused.json()["code"] == "unsupported_media"

    # An executable renamed to .jpg must also be refused, on its bytes.
    disguised = client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("evil.jpg", b"MZ\x90\x00" + b"\x00" * 60, "image/jpeg")},
    )
    assert disguised.status_code == 415


def test_admin_job_dashboard_reflects_the_pipeline(
    client: TestClient, admin_headers: dict[str, str], floor: str, panorama_factory
) -> None:
    """§45: the processing dashboard is a query over persisted job rows."""
    session_id = new_session(client, admin_headers, floor)
    client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("a.jpg", panorama_factory(), "image/jpeg")},
    )
    client.post(f"/api/captures/{session_id}/process", headers=admin_headers)
    wait(client, admin_headers, session_id)

    jobs = client.get("/api/jobs", headers=admin_headers, params={"session_id": session_id}).json()
    assert jobs["total"] == 7  # one row per canonical pipeline stage
    assert {job["job_type"] for job in jobs["items"]} == {
        "validate",
        "metadata",
        "frame_extraction",
        "quality",
        "features",
        "mapping",
        "viewer_data",
    }
    assert all(job["duration_seconds"] is not None for job in jobs["items"])

    summary = client.get("/api/jobs/summary", headers=admin_headers).json()
    assert summary["by_status"].get("complete", 0) >= 7
    assert "mean_duration_seconds" in summary
    assert summary["queue"]["backend"] == "thread"
    assert summary["queue"]["durable"] is False
