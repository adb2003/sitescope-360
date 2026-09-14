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
