"""Coverage, capture frequency, stale areas, and the §46 honesty guarantees."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient


def add_session_with_frames(
    db,
    *,
    session_id: str,
    project_id: str,
    floor_id: str,
    started_at: datetime,
    location_codes: list[str],
    location_coords: list[tuple[float, float]] | None = None,
):
    """Insert a ready capture session with one located frame per code.

    Builds the real row graph — session → file → frame → location — rather than shortcutting it.
    ``capture_frames.file_id`` is non-nullable by design (a frame always comes from something),
    so a fixture that skipped the file would only prove the constraint works.
    """
    from app.core.enums import CaptureSessionStatus, DataProvenance
    from app.models import CaptureFile, CaptureFrame, CaptureLocation, CaptureSession

    session = CaptureSession(
        id=session_id,
        project_id=project_id,
        floor_id=floor_id,
        started_at=started_at,
        status=CaptureSessionStatus.READY,
        frame_count=len(location_codes),
        file_count=1,
    )
    db.add(session)
    db.flush()

    capture_file = CaptureFile(
        session_id=session.id,
        original_filename="seed.jpg",
        storage_key=f"projects/{project_id}/floors/{floor_id}/sessions/{session_id}/source/a.jpg",
        content_type="image/jpeg",
        file_size_bytes=1024,
    )
    db.add(capture_file)
    db.flush()

    coords = location_coords or [(100.0 + i * 50, 100.0) for i in range(len(location_codes))]
    locations = []
    for index, code in enumerate(location_codes):
        location = CaptureLocation(
            floor_id=floor_id,
            code=code,
            x=coords[index][0],
            y=coords[index][1],
            provenance=DataProvenance.MANUAL,
        )
        db.add(location)
        db.flush()
        locations.append(location)

        db.add(
            CaptureFrame(
                session_id=session.id,
                file_id=capture_file.id,
                sequence_index=index,
                storage_key=(
                    f"projects/{project_id}/floors/{floor_id}/sessions/{session_id}"
                    f"/frames/{index}.jpg"
                ),
                width_px=2048,
                height_px=1024,
                captured_at=started_at,
                location_id=location.id,
            )
        )
    db.commit()
    return session, locations


@pytest.fixture
def project_tree(client: TestClient, admin_headers: dict[str, str]) -> dict[str, str]:
    """A project with one building, two floors and declared zones."""
    project_id = client.post(
        "/api/projects",
        headers=admin_headers,
        json={"name": "Monitoring", "code": "MON-1", "expected_captures_per_week": 2},
    ).json()["id"]
    building_id = client.post(
        f"/api/projects/{project_id}/buildings", headers=admin_headers, json={"name": "B"}
    ).json()["id"]

    floor_a = client.post(
        f"/api/buildings/{building_id}/floors",
        headers=admin_headers,
        json={"name": "Ground", "level": 0, "area_sqft": 10000.0},
    ).json()["id"]
    floor_b = client.post(
        f"/api/buildings/{building_id}/floors",
        headers=admin_headers,
        json={"name": "Level 01", "level": 1, "area_sqft": 8000.0},
    ).json()["id"]

    # Ground: two zones expecting 4 points each → 8 expected points.
    for name in ("North", "South"):
        client.post(
            f"/api/floors/{floor_a}/zones",
            headers=admin_headers,
            json={"name": name, "area_sqft": 5000.0, "expected_capture_points": 4},
        )

    return {"project_id": project_id, "floor_a": floor_a, "floor_b": floor_b}


def test_coverage_is_zero_and_honest_before_any_capture(
    client: TestClient, admin_headers: dict[str, str], project_tree: dict[str, str]
) -> None:
    coverage = client.get(
        f"/api/projects/{project_tree['project_id']}/coverage", headers=admin_headers
    ).json()

    assert coverage["coverage_ratio"] == 0.0
    assert coverage["provenance"] == "manual"
    assert "administrator-declared" in coverage["method"]
    assert "not measured from imagery" in coverage["method"]

    ground = next(f for f in coverage["floors"] if f["floor_name"] == "Ground")
    assert ground["expected_capture_points"] == 8
    assert ground["visited_locations"] == 0
    assert ground["covered_area_sqft"] == 0.0
    assert ground["pending_area_sqft"] == 10000.0


def test_dashboard_reports_empty_rather_than_confident_zeroes(
    client: TestClient, admin_headers: dict[str, str], project_tree: dict[str, str]
) -> None:
    """§46: an absent number must read as absent, not as a measured zero."""
    dashboard = client.get(
        f"/api/projects/{project_tree['project_id']}/dashboard", headers=admin_headers
    ).json()

    assert dashboard["is_empty"] is True
    assert dashboard["frame_count"] == 0
    assert dashboard["last_capture_at"] is None
    assert dashboard["frequency"]["status"] == "never_captured"
    assert dashboard["frequency"]["days_since_last_capture"] is None
    assert dashboard["frequency"]["next_expected_date"] is None


def test_coverage_reflects_partial_placement(
    client: TestClient,
    db,
    admin_headers: dict[str, str],
    project_tree: dict[str, str],
) -> None:
    """Visiting 2 of 8 declared points is 25% — apportioned across the floor's area."""
    add_session_with_frames(
        db,
        session_id="CAP-20260901-100000-AAAA",
        project_id=project_tree["project_id"],
        floor_id=project_tree["floor_a"],
        started_at=datetime.now(UTC) - timedelta(days=1),
        location_codes=["P01", "P02"],
    )

    coverage = client.get(
        f"/api/projects/{project_tree['project_id']}/coverage", headers=admin_headers
    ).json()
    ground = next(f for f in coverage["floors"] if f["floor_name"] == "Ground")

    assert ground["visited_locations"] == 2
    assert ground["expected_capture_points"] == 8
    assert ground["coverage_ratio"] == 0.25
    assert ground["covered_area_sqft"] == 2500.0
    assert ground["pending_area_sqft"] == 7500.0


def test_frequency_progresses_from_ok_to_due_to_overdue(
    client: TestClient,
    db,
    admin_headers: dict[str, str],
    project_tree: dict[str, str],
) -> None:
    """§20: two per week means one every 3.5 days, plus a grace day before overdue."""
    from app.models import CaptureSession

    project_id = project_tree["project_id"]
    floor_a = project_tree["floor_a"]

    def set_last_capture(days_ago: float) -> dict:
        db.query(CaptureSession).delete()
        db.add(
            CaptureSession(
                id=f"CAP-{int(days_ago * 100):08d}-100000-BBBB",
                project_id=project_id,
                floor_id=floor_a,
                started_at=datetime.now(UTC) - timedelta(days=days_ago),
                status="ready",
            )
        )
        db.commit()
        return client.get(f"/api/projects/{project_id}/frequency", headers=admin_headers).json()

    fresh = set_last_capture(1.0)
    assert fresh["status"] == "ok"
    assert fresh["days_since_last_capture"] == 1
    assert fresh["expected_per_week"] == 2
    assert fresh["next_expected_date"] is not None

    due = set_last_capture(4.0)  # past the 3.5-day interval, inside the grace day
    assert due["status"] == "due"

    overdue = set_last_capture(9.0)
    assert overdue["status"] == "overdue"
    assert overdue["days_since_last_capture"] == 9


def test_schedule_override_changes_the_verdict(
    client: TestClient,
    db,
    admin_headers: dict[str, str],
    project_tree: dict[str, str],
) -> None:
    """An administrator-configured cadence must actually drive the calculation."""
    from app.models import CaptureSession

    project_id = project_tree["project_id"]
    db.add(
        CaptureSession(
            id="CAP-20260901-100000-CCCC",
            project_id=project_id,
            floor_id=project_tree["floor_a"],
            started_at=datetime.now(UTC) - timedelta(days=5),
            status="ready",
        )
    )
    db.commit()

    # At 2/week (every 3.5 days) a 5-day-old capture is overdue.
    assert (
        client.get(f"/api/projects/{project_id}/frequency", headers=admin_headers).json()["status"]
        == "overdue"
    )

    # Relax to one per fortnight-ish: 1/week → every 7 days, so 5 days is fine.
    updated = client.put(
        f"/api/projects/{project_id}/schedule",
        headers=admin_headers,
        json={"expected_per_week": 1, "grace_days": 1},
    )
    assert updated.status_code == 200

    relaxed = client.get(f"/api/projects/{project_id}/frequency", headers=admin_headers).json()
    assert relaxed["expected_per_week"] == 1
    assert relaxed["status"] == "ok"


def test_stale_areas_separates_never_captured_from_stale(
    client: TestClient,
    db,
    admin_headers: dict[str, str],
    project_tree: dict[str, str],
) -> None:
    """Product vision item 11: identify areas not captured recently."""
    from app.core.enums import DataProvenance
    from app.models import CaptureLocation

    floor_a = project_tree["floor_a"]

    # P01 was captured 30 days ago → stale.
    add_session_with_frames(
        db,
        session_id="CAP-20260801-100000-DDDD",
        project_id=project_tree["project_id"],
        floor_id=floor_a,
        started_at=datetime.now(UTC) - timedelta(days=30),
        location_codes=["P01"],
    )
    # P02 exists on the plan but has never been visited.
    db.add(
        CaptureLocation(
            floor_id=floor_a, code="P02", x=20.0, y=20.0, provenance=DataProvenance.MANUAL
        )
    )
    db.commit()

    stale = client.get(
        f"/api/projects/{project_tree['project_id']}/stale-areas",
        headers=admin_headers,
        params={"threshold_days": 14},
    ).json()

    assert stale["threshold_days"] == 14
    assert [item["code"] for item in stale["never_captured"]] == ["P02"]
    assert [item["code"] for item in stale["stale"]] == ["P01"]
    assert stale["stale"][0]["days_since_capture"] == 30
    assert stale["never_captured"][0]["days_since_capture"] is None


def test_timeline_states_its_denominator(
    client: TestClient, admin_headers: dict[str, str], project_tree: dict[str, str]
) -> None:
    """A coverage ratio is meaningless without the denominator, so it is returned."""
    timeline = client.get(
        f"/api/projects/{project_tree['project_id']}/timeline", headers=admin_headers
    ).json()

    assert timeline["location_total"] == 0
    assert timeline["entries"] == []
    assert timeline["note"] is not None
    assert "no capture locations" in timeline["note"].lower()


def test_portfolio_counts_overdue_projects(
    client: TestClient, admin_headers: dict[str, str], project_tree: dict[str, str]
) -> None:
    portfolio = client.get("/api/dashboard", headers=admin_headers).json()

    assert portfolio["total_projects"] == 1
    assert portfolio["total_sessions"] == 0
    row = portfolio["projects"][0]
    assert row["project_code"] == "MON-1"
    assert row["frequency_status"] == "never_captured"
    assert row["coverage_ratio"] == 0.0


def test_system_capabilities_declares_what_is_not_implemented(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """§46: unimplemented analysis must be reported as unavailable, never faked."""
    capabilities = client.get("/api/system/capabilities", headers=admin_headers).json()

    vision = capabilities["vision"]
    assert vision["image_quality"]["implemented"] is True
    assert vision["image_quality"]["provenance"] == "measured"
    assert vision["feature_matching"]["implemented"] is True
    assert vision["relative_positioning"]["implemented"] is True
    assert vision["relative_positioning"]["provenance"] == "estimated"
    assert "no metric scale — direction only" in vision["relative_positioning"]["limitations"]
    assert (
        "not SLAM; no loop closure or global optimisation"
        in (vision["relative_positioning"]["limitations"])
    )

    for feature in ("object_detection", "defect_detection", "progress_detection"):
        assert vision[feature]["implemented"] is False, feature
        assert vision[feature]["phase"] == 3

    assert capabilities["bim"]["implemented"] is False
    assert capabilities["bim"]["interfaces_defined"]
    assert capabilities["bim"]["still_required"]

    assert capabilities["provenance_levels"] == [
        "measured",
        "estimated",
        "ai_predicted",
        "manual",
    ]

    # The independence statement is part of the contract, not decoration.
    assert capabilities["independence"]["third_party_monitoring_platforms"] == []
    assert "no OpenSpace" in capabilities["independence"]["statement"]


def test_progress_detection_refuses_rather_than_estimates() -> None:
    """§46: no fabricated progress percentage, and it must say what is missing."""
    from app.vision.ai import UnavailableAnalysis, get_ai_engine

    engine = get_ai_engine()
    result = engine.progress_detection.compare(None, None)  # type: ignore[arg-type]

    assert isinstance(result, UnavailableAnalysis)
    assert result.available is False
    assert "Phase 3" in result.reason
    assert result.required


def test_object_and_defect_detection_raise_rather_than_return_fake_results() -> None:
    from app.vision.ai import get_ai_engine

    engine = get_ai_engine()
    with pytest.raises(NotImplementedError, match="not implemented"):
        engine.object_detection.detect(None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError, match="not implemented"):
        engine.defect_detection.detect(None)  # type: ignore[arg-type]


def test_bim_interfaces_are_declared_but_unimplemented() -> None:
    """§30: interfaces exist so the schema can be verified; nothing pretends to work."""
    from app.bim import BimNotImplementedError, GeometricBimComparator, IfcBimSource, bim_status

    assert IfcBimSource().supported_extensions() == (".ifc",)
    with pytest.raises(BimNotImplementedError):
        IfcBimSource().load("model.ifc")
    with pytest.raises(BimNotImplementedError):
        GeometricBimComparator().compare([], None, [])  # type: ignore[arg-type]

    status = bim_status()
    assert status["implemented"] is False
    assert "floor_plans.px_per_meter" in status["schema_ready"]


def test_camera_providers_declare_their_real_capabilities(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """§6/§46: an unverified adapter must not claim to be implemented."""
    providers = {
        item["key"]: item
        for item in client.get("/api/cameras/providers", headers=admin_headers).json()
    }

    assert providers["local_upload"]["implemented"] is True
    assert providers["folder_import"]["implemented"] is True
    # OSC and the vendor adapter are honest about being unverified / absent.
    assert providers["osc"]["implemented"] is False
    assert providers["insta360"]["implemented"] is False
    assert "vendor SDK" in providers["insta360"]["description"]
    # The template must not be offered as a choice.
    assert "template" not in providers


def test_health_endpoint_needs_no_authentication(client: TestClient) -> None:
    """Container probes cannot present a token."""
    assert client.get("/health").status_code == 200
    body = client.get("/api/system/health").json()
    assert body["status"] == "ok"
    assert body["database"]["ok"] is True


def test_pagination_caps_the_page_size(
    client: TestClient, admin_headers: dict[str, str], project_tree: dict[str, str]
) -> None:
    """§35: no endpoint may be asked to return an unbounded page."""
    over_limit = client.get("/api/projects", headers=admin_headers, params={"limit": 5000})
    assert over_limit.status_code == 422

    ok = client.get("/api/projects", headers=admin_headers, params={"limit": 1})
    assert ok.status_code == 200
    body = ok.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["limit"] == 1


def test_unknown_ids_return_404_not_500(client: TestClient, admin_headers: dict[str, str]) -> None:
    for path in (
        "/api/projects/does-not-exist",
        "/api/floors/does-not-exist",
        "/api/captures/does-not-exist",
        "/api/frames/does-not-exist",
        "/api/reports/does-not-exist",
    ):
        response = client.get(path, headers=admin_headers)
        assert response.status_code == 404, f"{path} → {response.status_code}"
        assert response.json()["code"] == "not_found"


def test_project_delete_requires_the_code_as_confirmation(
    client: TestClient, admin_headers: dict[str, str], project_tree: dict[str, str]
) -> None:
    """Destroying a site's capture history should not be one mistyped id away."""
    project_id = project_tree["project_id"]

    wrong = client.delete(
        f"/api/projects/{project_id}", headers=admin_headers, params={"confirm_code": "WRONG"}
    )
    assert wrong.status_code == 422
    assert client.get(f"/api/projects/{project_id}", headers=admin_headers).status_code == 200

    correct = client.delete(
        f"/api/projects/{project_id}", headers=admin_headers, params={"confirm_code": "MON-1"}
    )
    assert correct.status_code == 200
    assert client.get(f"/api/projects/{project_id}", headers=admin_headers).status_code == 404


def test_report_formats_share_the_same_figures(
    client: TestClient,
    db,
    admin_headers: dict[str, str],
    project_tree: dict[str, str],
) -> None:
    """A stored payload lets one computation be re-rendered, so formats cannot disagree."""
    project_id = project_tree["project_id"]

    json_report = client.post(
        f"/api/projects/{project_id}/reports",
        headers=admin_headers,
        json={"report_type": "floor_coverage", "report_format": "json"},
    ).json()
    detail = client.get(f"/api/reports/{json_report['id']}", headers=admin_headers).json()

    assert detail["payload"]["report_type"] == "floor_coverage"
    assert "provenance" in detail["payload"]
    assert detail["payload"]["overall_ratio"] == 0.0

    downloaded = client.get(
        f"/api/reports/{json_report['id']}/download", headers=admin_headers
    ).json()
    assert downloaded["overall_ratio"] == detail["payload"]["overall_ratio"]
    assert downloaded["method"] == detail["payload"]["method"]


def test_report_types_are_advertised(client: TestClient, admin_headers: dict[str, str]) -> None:
    body = client.get("/api/report-types", headers=admin_headers).json()
    keys = {item["key"] for item in body["types"]}
    assert keys == {
        "project_summary",
        "floor_coverage",
        "capture_history",
        "annotation_log",
    }
    assert set(body["formats"]) == {"pdf", "csv", "json"}
