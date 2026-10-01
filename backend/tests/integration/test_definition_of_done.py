"""The §52 Definition of Done, executed end to end through the HTTP API.

    LOGIN → CREATE PROJECT → CREATE BUILDING → CREATE FLOOR → UPLOAD FLOOR PLAN
      → CREATE CAPTURE SESSION → UPLOAD 360 IMAGE → PROCESS CAPTURE → VIEW 360 IMAGE
      → PLACE CAPTURE ON FLOOR PLAN → OPEN CAPTURE FROM FLOOR PLAN → UPLOAD ANOTHER CAPTURE
      → VIEW CAPTURE HISTORY → COMPARE TWO DATES → SEE CAPTURE PATH → SEE COVERAGE
      → SEE LAST CAPTURE / OVERDUE STATUS → ADD ANNOTATION → GENERATE BASIC REPORT

One test, in order, sharing state — because the requirement is that the *whole workflow* works,
not that eighteen endpoints each work in isolation. Every step asserts on the response the next
step depends on, so a break is reported at the step that caused it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.enums import CaptureSessionStatus


def test_full_mvp_workflow(
    client: TestClient,
    db,
    admin,
    engineer,
    panorama_factory,
) -> None:
    from app.processing.floorplan_render import render_floor_plan
    from app.processing.synthetic import Room, default_walk

    room = Room()

    # ---- 1. LOGIN -------------------------------------------------------
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@test.example.com", "password": "test-password-1234"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "admin"
    assert "project:write" in me.json()["capabilities"]

    # ---- 2. CREATE PROJECT ----------------------------------------------
    project_response = client.post(
        "/api/projects",
        headers=headers,
        json={
            "name": "Chevron Phase 2",
            "code": "CVX-P2",
            "client": "Chevron",
            "location": "Bakersfield",
            "expected_captures_per_week": 2,
        },
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["id"]

    # ---- 3. CREATE BUILDING ---------------------------------------------
    building_response = client.post(
        f"/api/projects/{project_id}/buildings",
        headers=headers,
        json={"name": "Main Office"},
    )
    assert building_response.status_code == 201, building_response.text
    building_id = building_response.json()["id"]

    # ---- 4. CREATE FLOOR ------------------------------------------------
    floor_response = client.post(
        f"/api/buildings/{building_id}/floors",
        headers=headers,
        json={"name": "Level 03", "level": 3, "area_sqft": 10000.0},
    )
    assert floor_response.status_code == 201, floor_response.text
    floor_id = floor_response.json()["id"]

    zone_response = client.post(
        f"/api/floors/{floor_id}/zones",
        headers=headers,
        json={"name": "North Wing", "area_sqft": 4000.0, "expected_capture_points": 4},
    )
    assert zone_response.status_code == 201, zone_response.text
    zone_id = zone_response.json()["id"]

    # ---- 5. UPLOAD FLOOR PLAN -------------------------------------------
    plan_png, plan_w, plan_h, _ = render_floor_plan(room, title="Level 03")
    plan_response = client.post(
        f"/api/floors/{floor_id}/floorplan",
        headers=headers,
        files={"file": ("level03.png", plan_png, "image/png")},
        data={"name": "Level 03 GA"},
    )
    assert plan_response.status_code == 201, plan_response.text
    plan = plan_response.json()
    assert plan["width_px"] == plan_w
    assert plan["height_px"] == plan_h
    # The asset is exposed as a signed URL, never a filesystem path (§44).
    assert plan["asset"]["url"].startswith("http")
    assert "signature=" in plan["asset"]["url"]
    assert "/var/" not in plan["asset"]["url"]

    # The signed URL must actually serve the bytes.
    asset_path = plan["asset"]["url"].removeprefix("http://testserver")
    asset_response = client.get(asset_path)
    assert asset_response.status_code == 200
    assert asset_response.content[:8] == b"\x89PNG\r\n\x1a\n"

    # ---- 6. CREATE CAPTURE SESSION --------------------------------------
    first_visit = datetime.now(UTC) - timedelta(days=7)
    session_response = client.post(
        "/api/captures",
        headers=headers,
        json={
            "floor_id": floor_id,
            "zone_id": zone_id,
            "name": "07 September — Morning",
            "started_at": first_visit.isoformat(),
        },
    )
    assert session_response.status_code == 201, session_response.text
    session_one = session_response.json()
    assert session_one["id"].startswith("CAP-")
    assert session_one["status"] == CaptureSessionStatus.DRAFT
    session_one_id = session_one["id"]

    # ---- 7. UPLOAD 360 IMAGES -------------------------------------------
    positions = default_walk(room, step_m=1.5)[:4]
    uploads = [
        ("files", (f"PANO_{index:03d}.jpg", panorama_factory(pos, brightness=0.9), "image/jpeg"))
        for index, pos in enumerate(positions)
    ]
    upload_response = client.post(
        f"/api/captures/{session_one_id}/upload", headers=headers, files=uploads
    )
    assert upload_response.status_code == 201, upload_response.text
    assert len(upload_response.json()) == 4

    # ---- 8. PROCESS CAPTURE ---------------------------------------------
    process_response = client.post(f"/api/captures/{session_one_id}/process", headers=headers)
    assert process_response.status_code == 200, process_response.text

    status = _await_ready(client, headers, session_one_id)
    assert status["status"] == CaptureSessionStatus.READY, status
    assert status["progress"] == 1.0
    stages = {stage["job_type"]: stage["status"] for stage in status["stages"]}
    assert stages == {
        "validate": "complete",
        "metadata": "complete",
        "frame_extraction": "complete",
        "quality": "complete",
        "features": "complete",
        "mapping": "complete",
        "viewer_data": "complete",
    }, stages

    # ---- 9. VIEW 360 IMAGE ----------------------------------------------
    frames_response = client.get(f"/api/captures/{session_one_id}/frames", headers=headers)
    assert frames_response.status_code == 200, frames_response.text
    frames = frames_response.json()["items"]
    assert len(frames) == 4

    first_frame = frames[0]
    assert first_frame["projection"] == "equirectangular"
    assert first_frame["quality"] is not None
    assert first_frame["quality"]["verdict"] in {"good", "warning"}
    assert first_frame["quality"]["is_usable"] is True
    assert first_frame["thumbnail"]["url"].startswith("http")
    assert first_frame["preview"]["url"].startswith("http")
    assert first_frame["full"]["url"].startswith("http")

    viewer_response = client.get(f"/api/frames/{first_frame['id']}/viewer", headers=headers)
    assert viewer_response.status_code == 200, viewer_response.text
    manifest = viewer_response.json()
    assert manifest["frame"]["id"] == first_frame["id"]
    assert manifest["next_frame_id"] == frames[1]["id"]
    assert manifest["previous_frame_id"] is None
    # Neighbours come from estimated relative positions, so there should be some.
    assert len(manifest["neighbours"]) >= 1

    # ---- 10. PLACE CAPTURE ON FLOOR PLAN --------------------------------
    location_ids: list[str] = []
    for index, frame in enumerate(frames):
        x_px = 200.0 + index * 60.0
        y_px = 300.0 + index * 25.0
        placed = client.put(
            f"/api/frames/{frame['id']}/location",
            headers=headers,
            json={"x": x_px, "y": y_px},
        )
        assert placed.status_code == 200, placed.text
        body = placed.json()
        assert body["location_id"] is not None
        assert body["position_provenance"] == "manual"
        location_ids.append(body["location_id"])

    locations_response = client.get(f"/api/floors/{floor_id}/locations", headers=headers)
    assert locations_response.status_code == 200
    locations = locations_response.json()
    assert [item["code"] for item in locations] == ["P01", "P02", "P03", "P04"]
    assert all(item["frame_count"] == 1 for item in locations)

    # ---- 11. OPEN CAPTURE FROM FLOOR PLAN -------------------------------
    # Clicking P03 on the plan must resolve to its panorama.
    p03 = next(item for item in locations if item["code"] == "P03")
    assert p03["latest_frame_id"] is not None
    from_plan = client.get(f"/api/locations/{p03['id']}/frames", headers=headers)
    assert from_plan.status_code == 200, from_plan.text
    assert from_plan.json()["items"][0]["id"] == p03["latest_frame_id"]
    assert from_plan.json()["items"][0]["full"]["url"].startswith("http")

    # ---- 12. UPLOAD ANOTHER CAPTURE (a later date, same locations) ------
    second_visit = datetime.now(UTC)
    session_two_id = client.post(
        "/api/captures",
        headers=headers,
        json={
            "floor_id": floor_id,
            "zone_id": zone_id,
            "name": "Today — Morning",
            "started_at": second_visit.isoformat(),
        },
    ).json()["id"]

    # Brighter render: the same room on a different day, so compare shows a real difference.
    later_uploads = [
        ("files", (f"PANO2_{index:03d}.jpg", panorama_factory(pos, brightness=1.12), "image/jpeg"))
        for index, pos in enumerate(positions)
    ]
    assert (
        client.post(
            f"/api/captures/{session_two_id}/upload", headers=headers, files=later_uploads
        ).status_code
        == 201
    )
    assert (
        client.post(f"/api/captures/{session_two_id}/process", headers=headers).status_code == 200
    )
    assert _await_ready(client, headers, session_two_id)["status"] == CaptureSessionStatus.READY

    second_frames = client.get(f"/api/captures/{session_two_id}/frames", headers=headers).json()[
        "items"
    ]
    for frame, location_id in zip(second_frames, location_ids, strict=True):
        placed = client.put(
            f"/api/frames/{frame['id']}/location",
            headers=headers,
            json={"location_id": location_id},
        )
        assert placed.status_code == 200, placed.text

    # ---- 13. VIEW CAPTURE HISTORY ---------------------------------------
    history = client.get(f"/api/locations/{location_ids[2]}/frames", headers=headers).json()
    assert history["total"] == 2, "P03 should now have one frame from each visit"
    assert history["items"][0]["captured_at"] > history["items"][1]["captured_at"]

    timeline = client.get(f"/api/projects/{project_id}/timeline", headers=headers).json()
    assert timeline["location_total"] == 4
    assert len(timeline["entries"]) == 2
    assert timeline["entries"][0]["coverage_ratio"] == 1.0
    assert timeline["entries"][0]["coverage_provenance"] == "measured"

    # ---- 14. COMPARE TWO DATES ------------------------------------------
    compare = client.get(f"/api/compare/locations/{location_ids[2]}", headers=headers)
    assert compare.status_code == 200, compare.text
    comparison = compare.json()
    assert comparison["same_location"] is True
    assert comparison["note"] is None
    assert comparison["days_apart"] == 7
    assert comparison["a"]["id"] != comparison["b"]["id"]
    assert comparison["a"]["full"]["url"] and comparison["b"]["full"]["url"]

    # ---- 15. SEE CAPTURE PATH -------------------------------------------
    path = client.get(f"/api/captures/{session_one_id}/path", headers=headers)
    assert path.status_code == 200, path.text
    steps = path.json()["steps"]
    assert [step["code"] for step in steps] == ["P01", "P02", "P03", "P04"]
    assert path.json()["provenance"] == "manual"
    # Steps must be ordered by timestamp, as §13 requires.
    assert steps == sorted(steps, key=lambda step: step["timestamp"])

    floor_paths = client.get(f"/api/floors/{floor_id}/paths", headers=headers).json()
    assert floor_paths["total"] == 2, "one path per visit"

    # ---- 16. SEE COVERAGE -----------------------------------------------
    coverage = client.get(f"/api/projects/{project_id}/coverage", headers=headers).json()
    assert coverage["provenance"] == "manual"
    assert "not measured from imagery" in coverage["method"]
    floor_coverage = coverage["floors"][0]
    assert floor_coverage["visited_locations"] == 4
    assert floor_coverage["expected_capture_points"] == 4
    assert floor_coverage["coverage_ratio"] == 1.0
    assert floor_coverage["covered_area_sqft"] == 10000.0

    # ---- 17. SEE LAST CAPTURE / OVERDUE STATUS --------------------------
    frequency = client.get(f"/api/projects/{project_id}/frequency", headers=headers).json()
    assert frequency["expected_per_week"] == 2
    assert frequency["last_capture_at"] is not None
    assert frequency["days_since_last_capture"] == 0
    assert frequency["status"] == "ok"
    assert frequency["captures_this_week"] == 1

    # ---- 18. ADD ANNOTATION ---------------------------------------------
    annotation_response = client.post(
        f"/api/frames/{second_frames[1]['id']}/annotations",
        headers=headers,
        json={
            "title": "Missing ceiling panel",
            "description": "Panel absent above grid C4.",
            "yaw_deg": 42.0,
            "pitch_deg": 12.0,
            "priority": "high",
        },
    )
    assert annotation_response.status_code == 201, annotation_response.text
    annotation = annotation_response.json()
    assert annotation["status"] == "open"
    assert annotation["created_by_name"] == "Test Admin"
    assert annotation["location_code"] == "P02"

    listed = client.get(f"/api/projects/{project_id}/annotations", headers=headers).json()
    assert listed["total"] == 1

    # ---- 19. GENERATE BASIC REPORT --------------------------------------
    report_response = client.post(
        f"/api/projects/{project_id}/reports",
        headers=headers,
        json={"report_type": "project_summary", "report_format": "pdf"},
    )
    assert report_response.status_code == 201, report_response.text
    report = report_response.json()
    assert report["status"] == "ready"
    assert report["file_size_bytes"] > 1000

    download = client.get(f"/api/reports/{report['id']}/download", headers=headers)
    assert download.status_code == 200
    assert download.content[:5] == b"%PDF-"
    assert "attachment" in download.headers["content-disposition"]

    # CSV of the same report must reuse the same figures.
    csv_report = client.post(
        f"/api/projects/{project_id}/reports",
        headers=headers,
        json={"report_type": "capture_history", "report_format": "csv"},
    ).json()
    csv_body = client.get(f"/api/reports/{csv_report['id']}/download", headers=headers).text
    assert "Capture history" in csv_body
    assert session_one_id in csv_body or "Level 03" in csv_body
    # The provenance caveats must survive the CSV export.
    assert "not measured from imagery" in csv_body

    # ---- final: the dashboard reflects everything above -----------------
    dashboard = client.get(f"/api/projects/{project_id}/dashboard", headers=headers).json()
    assert dashboard["is_empty"] is False
    assert dashboard["session_count"] == 2
    assert dashboard["frame_count"] == 8
    assert dashboard["location_count"] == 4
    assert dashboard["building_count"] == 1
    assert dashboard["floor_count"] == 1
    assert dashboard["zone_count"] == 1
    assert dashboard["annotation_open_count"] == 1
    assert dashboard["capture_health"]["completed"] == 2
    assert dashboard["capture_health"]["failed"] == 0
    assert dashboard["coverage"]["coverage_ratio"] == 1.0
    assert dashboard["quality"]["unscored"] == 0
    assert len(dashboard["recent_captures"]) == 2
    assert dashboard["floors"][0]["coverage_ratio"] == 1.0


def _await_ready(
    client: TestClient, headers: dict[str, str], session_id: str, attempts: int = 200
) -> dict:
    """Poll the status endpoint until processing settles.

    The thread queue runs stages in a background pool, so the API returns before work finishes —
    exactly as it will in production with Redis. Polling the real status endpoint is therefore
    the honest way to wait, and it exercises the endpoint the UI uses.
    """
    import time

    for _ in range(attempts):
        response = client.get(f"/api/captures/{session_id}/status", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in {CaptureSessionStatus.READY, CaptureSessionStatus.FAILED}:
            return body
        time.sleep(0.1)
    raise AssertionError(f"Capture {session_id} did not finish processing in time")
