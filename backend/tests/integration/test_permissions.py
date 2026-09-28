"""Role enforcement at the HTTP boundary (§27).

Asserting the permission table through the API, not just the capability sets, because a correct
matrix wired to the wrong route protects nothing.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import auth_headers, join_project, make_user


def _project(client: TestClient, headers: dict[str, str], code: str = "PERM-1") -> str:
    response = client.post(
        "/api/projects", headers=headers, json={"name": "Perm test", "code": code}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_unauthenticated_requests_are_refused(client: TestClient) -> None:
    for path in (
        "/api/projects",
        "/api/dashboard",
        "/api/captures",
        "/api/jobs",
        "/api/auth/me",
        "/api/cameras/providers",
        "/api/system/capabilities",
    ):
        response = client.get(path)
        assert response.status_code == 401, f"GET {path} → {response.status_code}"
        assert response.json()["code"] == "authentication_failed"

    # Authentication must be checked before the body is validated: an unauthenticated caller
    # should learn nothing about whether their payload was acceptable.
    posted = client.post("/api/projects", json={"name": "X", "code": "X1"})
    assert posted.status_code == 401
    assert posted.json()["code"] == "authentication_failed"


def test_malformed_bearer_token_is_refused(client: TestClient) -> None:
    for token in ("", "not-a-jwt", "a.b.c"):
        response = client.get("/api/projects", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


def test_viewer_can_read_but_not_write(
    client: TestClient, admin_headers: dict[str, str], viewer_headers: dict[str, str], viewer
) -> None:
    project_id = _project(client, admin_headers)
    # This test is about what a role may do, not which projects it can reach, so the viewer is
    # put on the project explicitly. Membership is enforced in tests as in production (§27).
    join_project(client, admin_headers, project_id, viewer.id)

    # Reads succeed.
    assert client.get("/api/projects", headers=viewer_headers).status_code == 200
    assert client.get(f"/api/projects/{project_id}", headers=viewer_headers).status_code == 200
    assert (
        client.get(f"/api/projects/{project_id}/dashboard", headers=viewer_headers).status_code
        == 200
    )

    # Writes are refused, with an explanation naming the missing capability.
    denied = client.post(
        "/api/projects", headers=viewer_headers, json={"name": "Nope", "code": "NOPE"}
    )
    assert denied.status_code == 403
    body = denied.json()
    assert body["code"] == "permission_denied"
    assert body["details"]["required_capability"] == "project:write"

    assert (
        client.post(
            f"/api/projects/{project_id}/buildings",
            headers=viewer_headers,
            json={"name": "B"},
        ).status_code
        == 403
    )


def test_site_engineer_can_capture_but_not_manage_projects(
    client: TestClient,
    db,
    admin_headers: dict[str, str],
    engineer_headers: dict[str, str],
    engineer,
) -> None:
    project_id = _project(client, admin_headers, "PERM-2")
    # About what the role may do, not which projects it reaches, so the engineer is put on this
    # one explicitly — membership is enforced in tests as in production (§27).
    join_project(client, admin_headers, project_id, engineer.id)
    building_id = client.post(
        f"/api/projects/{project_id}/buildings", headers=admin_headers, json={"name": "B"}
    ).json()["id"]
    floor_id = client.post(
        f"/api/buildings/{building_id}/floors",
        headers=admin_headers,
        json={"name": "L1", "level": 1},
    ).json()["id"]

    # Allowed: create a capture session.
    created = client.post("/api/captures", headers=engineer_headers, json={"floor_id": floor_id})
    assert created.status_code == 201, created.text

    # Refused: create a project.
    assert (
        client.post(
            "/api/projects", headers=engineer_headers, json={"name": "X", "code": "X1"}
        ).status_code
        == 403
    )
    # Refused: register a camera device (system settings).
    assert (
        client.post("/api/cameras", headers=engineer_headers, json={"label": "Cam"}).status_code
        == 403
    )
    # Refused: generate a report.
    assert (
        client.post(
            f"/api/projects/{project_id}/reports",
            headers=engineer_headers,
            json={"report_type": "project_summary", "report_format": "json"},
        ).status_code
        == 403
    )
    # Refused: the admin job dashboard.
    assert client.get("/api/jobs", headers=engineer_headers).status_code == 403


def test_viewer_cannot_upload_or_process(
    client: TestClient, admin_headers: dict[str, str], viewer_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_headers, "PERM-3")
    building_id = client.post(
        f"/api/projects/{project_id}/buildings", headers=admin_headers, json={"name": "B"}
    ).json()["id"]
    floor_id = client.post(
        f"/api/buildings/{building_id}/floors",
        headers=admin_headers,
        json={"name": "L1"},
    ).json()["id"]
    session_id = client.post(
        "/api/captures", headers=admin_headers, json={"floor_id": floor_id}
    ).json()["id"]

    assert (
        client.post(
            f"/api/captures/{session_id}/upload",
            headers=viewer_headers,
            files={"files": ("a.jpg", b"\xff\xd8\xff\x00", "image/jpeg")},
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/captures/{session_id}/process", headers=viewer_headers).status_code
        == 403
    )
    assert (
        client.post(
            "/api/captures", headers=viewer_headers, json={"floor_id": floor_id}
        ).status_code
        == 403
    )


def test_only_a_manager_can_resolve_someone_elses_annotation(
    client: TestClient,
    db,
    admin_headers: dict[str, str],
    engineer_headers: dict[str, str],
    engineer,
    panorama_factory,
) -> None:
    from app.core.enums import Role

    project_id = _project(client, admin_headers, "PERM-4")
    join_project(client, admin_headers, project_id, engineer.id)
    building_id = client.post(
        f"/api/projects/{project_id}/buildings", headers=admin_headers, json={"name": "B"}
    ).json()["id"]
    floor_id = client.post(
        f"/api/buildings/{building_id}/floors", headers=admin_headers, json={"name": "L1"}
    ).json()["id"]
    session_id = client.post(
        "/api/captures", headers=admin_headers, json={"floor_id": floor_id}
    ).json()["id"]
    client.post(
        f"/api/captures/{session_id}/upload",
        headers=admin_headers,
        files={"files": ("a.jpg", panorama_factory(), "image/jpeg")},
    )
    client.post(f"/api/captures/{session_id}/process", headers=admin_headers)

    import time

    for _ in range(200):
        status = client.get(f"/api/captures/{session_id}/status", headers=admin_headers).json()
        if status["status"] in {"ready", "failed"}:
            break
        time.sleep(0.1)
    assert status["status"] == "ready", status

    frame_id = client.get(f"/api/captures/{session_id}/frames", headers=admin_headers).json()[
        "items"
    ][0]["id"]

    # The engineer raises a finding.
    annotation_id = client.post(
        f"/api/frames/{frame_id}/annotations",
        headers=engineer_headers,
        json={"title": "Cracked slab", "priority": "high"},
    ).json()["id"]

    # A second engineer may not resolve it. On the project, so the refusal is the capability
    # check rather than membership — a non-member would get 404 and prove nothing about roles.
    other_engineer = make_user(db, "engineer2@test.example.com", Role.SITE_ENGINEER, "Other")
    join_project(client, admin_headers, project_id, other_engineer.id)
    other = auth_headers(client, "engineer2@test.example.com")
    refused = client.patch(
        f"/api/annotations/{annotation_id}", headers=other, json={"status": "resolved"}
    )
    assert refused.status_code == 403

    # The author may not resolve their own either — resolving is a management decision.
    self_resolve = client.patch(
        f"/api/annotations/{annotation_id}",
        headers=engineer_headers,
        json={"status": "resolved"},
    )
    assert self_resolve.status_code == 403

    # An admin can.
    resolved = client.patch(
        f"/api/annotations/{annotation_id}",
        headers=admin_headers,
        json={"status": "resolved"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_at"] is not None


def test_admin_cannot_lock_themselves_out(
    client: TestClient, admin_headers: dict[str, str], admin
) -> None:
    demote = client.patch(f"/api/users/{admin.id}", headers=admin_headers, json={"role": "viewer"})
    assert demote.status_code == 422
    assert "own admin role" in demote.json()["message"]

    deactivate = client.patch(
        f"/api/users/{admin.id}", headers=admin_headers, json={"is_active": False}
    )
    assert deactivate.status_code == 422


def test_a_role_change_invalidates_an_existing_token(
    client: TestClient,
    db,
    admin_headers: dict[str, str],
    engineer,
) -> None:
    """A token minted before a demotion must stop working immediately, not at expiry."""
    engineer_token = auth_headers(client, "engineer@test.example.com")
    assert client.get("/api/auth/me", headers=engineer_token).status_code == 200

    client.patch(f"/api/users/{engineer.id}", headers=admin_headers, json={"role": "viewer"})

    stale = client.get("/api/auth/me", headers=engineer_token)
    assert stale.status_code == 401
    assert "permissions changed" in stale.json()["message"]


def test_deactivated_user_cannot_authenticate(
    client: TestClient, db, admin_headers: dict[str, str], viewer
) -> None:
    token = auth_headers(client, "viewer@test.example.com")
    client.delete(f"/api/users/{viewer.id}", headers=admin_headers)

    assert client.get("/api/auth/me", headers=token).status_code == 401
    denied = client.post(
        "/api/auth/login",
        json={"email": "viewer@test.example.com", "password": "test-password-1234"},
    )
    assert denied.status_code == 401
    assert "deactivated" in denied.json()["message"]


def test_refresh_token_rotation_and_reuse_detection(client: TestClient, admin) -> None:
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@test.example.com", "password": "test-password-1234"},
    ).json()
    original = login["refresh_token"]

    rotated = client.post("/api/auth/refresh", json={"refresh_token": original})
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != original

    # The consumed token must not work twice.
    replay = client.post("/api/auth/refresh", json={"refresh_token": original})
    assert replay.status_code == 401


def test_logout_revokes_the_refresh_token(client: TestClient, admin) -> None:
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@test.example.com", "password": "test-password-1234"},
    ).json()

    assert (
        client.post("/api/auth/logout", json={"refresh_token": login["refresh_token"]}).status_code
        == 200
    )
    assert (
        client.post("/api/auth/refresh", json={"refresh_token": login["refresh_token"]}).status_code
        == 401
    )


def test_wrong_password_and_unknown_user_are_indistinguishable(client: TestClient, admin) -> None:
    wrong = client.post(
        "/api/auth/login",
        json={"email": "admin@test.example.com", "password": "definitely-wrong"},
    )
    missing = client.post(
        "/api/auth/login",
        json={"email": "nobody@test.example.com", "password": "definitely-wrong"},
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["message"] == missing.json()["message"]
