"""Nearest-capture-location search (§13).

The tests run on SQLite, so they exercise the portable branch of `app.services.spatial`. That is
deliberate rather than a limitation: the two branches are written to return *the same rows in the
same order with the same distances*, and PostGIS only changes the query plan. So the behaviour is
pinned here, and `test_postgis_spatial.py` is what pins the claim that PostgreSQL agrees.

What matters most below is the last group: "nearest" always finds something, however far away it
is, which is exactly how this kind of endpoint sends an operator to the wrong end of a building.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import auth_headers
from tests.integration.test_definition_of_done import _await_ready


def _floor(client: TestClient, headers: dict[str, str]) -> str:
    project = client.post(
        "/api/projects", json={"name": "Spatial", "code": "SPA-1"}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "Block A"}, headers=headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "Level 01", "level": 1, "area_sqft": 5000.0},
        headers=headers,
    ).json()
    return str(floor["id"])


def _place(client: TestClient, headers: dict[str, str], floor_id: str, code: str, x, y) -> str:
    response = client.post(
        f"/api/floors/{floor_id}/locations",
        json={"code": code, "x": x, "y": y},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _nearest(client: TestClient, headers: dict[str, str], floor_id: str, **params):
    response = client.get(
        f"/api/floors/{floor_id}/locations/nearest", params=params, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_closest_point_is_returned_with_its_distance(client: TestClient, admin) -> None:
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    _place(client, headers, floor_id, "A1", 0.0, 0.0)
    near = _place(client, headers, floor_id, "A2", 300.0, 400.0)

    found = _nearest(client, headers, floor_id, x=300.0, y=395.0)

    assert len(found) == 1
    assert found[0]["location"]["id"] == near
    assert found[0]["distance_px"] == 5.0


def test_distance_is_euclidean_not_axis_aligned(client: TestClient, admin) -> None:
    """A 3-4-5 triangle: Manhattan distance would say 7, and would rank points wrongly."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    _place(client, headers, floor_id, "A1", 3.0, 4.0)

    assert _nearest(client, headers, floor_id, x=0.0, y=0.0)[0]["distance_px"] == 5.0


def test_results_are_ordered_nearest_first(client: TestClient, admin) -> None:
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    # Inserted in the wrong order on purpose, so insertion order cannot pass for distance order.
    for code, x in [("C", 300.0), ("A", 10.0), ("D", 900.0), ("B", 100.0)]:
        _place(client, headers, floor_id, code, x, 0.0)

    found = _nearest(client, headers, floor_id, x=0.0, y=0.0, limit=4)

    assert [row["location"]["code"] for row in found] == ["A", "B", "C", "D"]
    distances = [row["distance_px"] for row in found]
    assert distances == sorted(distances)


def test_points_on_another_floor_are_never_returned(client: TestClient, admin) -> None:
    """Plan pixels are per-floor. Level 2's origin is not near Level 1's origin; it is unrelated."""
    headers = auth_headers(client, admin.email)
    floor_one = _floor(client, headers)
    building = client.post(
        f"/api/projects/{client.get('/api/projects', headers=headers).json()['items'][0]['id']}"
        "/buildings",
        json={"name": "Block B"},
        headers=headers,
    ).json()
    floor_two = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "Level 02", "level": 2, "area_sqft": 5000.0},
        headers=headers,
    ).json()["id"]

    _place(client, headers, floor_two, "B1", 0.0, 0.0)
    _place(client, headers, floor_one, "A1", 800.0, 800.0)

    found = _nearest(client, headers, floor_one, x=0.0, y=0.0)

    assert [row["location"]["code"] for row in found] == ["A1"]


def test_a_floor_with_no_locations_returns_nothing_rather_than_erroring(
    client: TestClient, admin
) -> None:
    headers = auth_headers(client, admin.email)
    assert _nearest(client, headers, _floor(client, headers), x=5.0, y=5.0) == []


def test_within_px_is_what_separates_a_hit_from_a_click_on_blank_paper(
    client: TestClient, admin
) -> None:
    """Without a radius, the far corner of a building is still "the nearest point".

    This is the failure the endpoint is most likely to cause in practice: an operator clicks an
    unphotographed corridor and gets jumped to a point 900px away, with nothing on screen saying
    the click missed.
    """
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    _place(client, headers, floor_id, "A1", 900.0, 900.0)

    # Unfiltered: something always comes back, however far.
    unfiltered = _nearest(client, headers, floor_id, x=0.0, y=0.0)
    assert len(unfiltered) == 1
    assert unfiltered[0]["distance_px"] > 1200

    assert _nearest(client, headers, floor_id, x=0.0, y=0.0, within_px=50.0) == []


def test_the_result_count_is_capped(client: TestClient, admin) -> None:
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    refused = client.get(
        f"/api/floors/{floor_id}/locations/nearest",
        params={"x": 0, "y": 0, "limit": 5000},
        headers=headers,
    )
    assert refused.status_code == 422


def test_an_unknown_floor_is_a_404_not_an_empty_list(client: TestClient, admin) -> None:
    """Empty would read as "this floor has no capture points", which is a different fact."""
    headers = auth_headers(client, admin.email)
    missing = client.get(
        "/api/floors/does-not-exist/locations/nearest",
        params={"x": 0, "y": 0},
        headers=headers,
    )
    assert missing.status_code == 404


# ---- time travel: what was captured near this click, across dates -------------------------
def _captures_near(client: TestClient, headers: dict[str, str], floor_id: str, **params):
    response = client.get(f"/api/floors/{floor_id}/captures-near", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_radius_is_applied_before_the_limit_not_after(client: TestClient, admin) -> None:
    """Otherwise "everything within R" quietly becomes "whichever of the nearest N fell inside".

    Five points sit inside a 100px radius. Asking for at most three must return three of them --
    not three candidates that were then filtered down to however many survived.
    """
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    for index in range(5):
        _place(client, headers, floor_id, f"R{index}", 10.0 * index, 0.0)
    # And one far outside, which must never appear.
    _place(client, headers, floor_id, "FAR", 5000.0, 5000.0)

    found = _captures_near(client, headers, floor_id, x=0.0, y=0.0, within_px=100.0, max_points=3)

    assert len(found) == 3
    assert "FAR" not in [row["location"]["code"] for row in found]


def test_a_click_where_nobody_walked_returns_nothing(client: TestClient, admin) -> None:
    """An empty list is the answer, not an error and not the far corner of the building."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    _place(client, headers, floor_id, "A1", 900.0, 900.0)

    assert _captures_near(client, headers, floor_id, x=0.0, y=0.0, within_px=50.0) == []


def test_a_point_exactly_on_the_radius_is_inside_it(client: TestClient, admin) -> None:
    """The boundary has to fall on one side deliberately, or it falls wherever `<=` was typed."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    _place(client, headers, floor_id, "EDGE", 30.0, 40.0)  # exactly 50 from the origin

    assert len(_captures_near(client, headers, floor_id, x=0.0, y=0.0, within_px=50.0)) == 1
    assert _captures_near(client, headers, floor_id, x=0.0, y=0.0, within_px=49.999) == []


def test_visits_come_back_newest_first_with_their_imagery(
    client: TestClient, admin, panorama_factory
) -> None:
    """The whole point of the query: the same spot on more than one date, most recent first."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    location_id = _place(client, headers, floor_id, "T1", 100.0, 100.0)

    # Two visits to the same point, processed and bound to it.
    for index, day in enumerate(("11", "12")):
        session_id = client.post(
            "/api/captures",
            json={
                "floor_id": floor_id,
                "client_session_id": f"CAP-202609{day}-100000-TT{index}",
                "started_at": f"2026-09-{day}T10:00:00Z",
            },
            headers=headers,
        ).json()["id"]
        client.post(
            f"/api/captures/{session_id}/upload",
            files={
                "files": (f"P{index}.jpg", panorama_factory((float(index), 0.0, 0.0)), "image/jpeg")
            },
            headers=headers,
        )
        client.post(f"/api/captures/{session_id}/finish", headers=headers)
        client.post(f"/api/captures/{session_id}/process", json={}, headers=headers)
        status = _await_ready(client, headers, session_id)
        assert status["status"] == "ready", status
        frame_id = client.get(f"/api/captures/{session_id}/frames", headers=headers).json()[
            "items"
        ][0]["id"]
        bound = client.put(
            f"/api/frames/{frame_id}/location",
            json={"location_id": location_id},
            headers=headers,
        )
        assert bound.status_code in (200, 201), bound.text

    found = _captures_near(client, headers, floor_id, x=105.0, y=105.0, within_px=50.0)

    assert len(found) == 1
    visits = found[0]["visits"]
    assert len(visits) == 2, visits
    # Newest first, and each one openable in the viewer.
    assert visits[0]["captured_at"] > visits[1]["captured_at"]
    assert all(visit["full"] and visit["full"]["url"] for visit in visits)
