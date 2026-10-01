"""Every list endpoint returns a page (§35).

`Page`'s own docstring has said "every list endpoint returns this" since it was written, and
three did not. They were the three that grow for as long as the product is used, which is the
worst three to miss:

* `/floors/{id}/paths` — one polyline per visit, each carrying its whole `steps` array at
  roughly 180 bytes per frame, fetched every time somebody opens the floor plan;
* `/locations/{id}/frames` — one frame per visit to a place, forever, and comparing dates at a
  place is the feature that makes people keep visiting;
* `/captures/{id}/files` — one row per uploaded file, and a day of stills is several hundred.

None of this is visible on a demo project. That is the point: an unpaginated list works
perfectly until the data arrives, and by then the response shape is what every client depends
on. The guard below is a single test over the whole route table rather than three assertions,
so the next list endpoint cannot be added without this decision being made again.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth_headers

#: Paths that return a bare array on purpose, with the reason.
#:
#: Each is bounded by something other than how long the product has been in use — a fixed
#: catalogue, a per-floor handful, or the payload of one request. A path is only allowed here
#: because somebody argued for it, which is the entire value of the list.
BARE_ARRAY_IS_FINE = {
    # Bounded by the building, not by how long it has been monitored. A tower has the floors it
    # has; capturing it for three years does not add any.
    "/api/projects/{project_id}/buildings": "buildings in one project",
    "/api/buildings/{building_id}/floors": "floors in one building",
    "/api/projects/{project_id}/floors": "every floor of one project",
    "/api/floors/{floor_id}/zones": "zones on one floor",
    # Bounded by the floor's coverage plan. Points converge on the number of places worth
    # standing, and all of them are drawn as pins at once — a partial page would be a plan with
    # pins missing and no way to tell.
    "/api/floors/{floor_id}/locations": "capture points on one floor, all drawn together",
    "/api/floors/{floor_id}/floorplans": "revisions of one floor's plan",
    # One expectation per floor plus a project default.
    "/api/projects/{project_id}/schedule": "declared capture cadence per floor",
    "/api/projects/{project_id}/members": "the people on one project",
    # Spatial queries that already carry their own radius and limit.
    "/api/floors/{floor_id}/locations/nearest": "a radius search with its own limit",
    "/api/floors/{floor_id}/captures-near": "a radius search with its own limit",
    # Bounded by one photograph: how many defects fit in a single frame.
    "/api/frames/{frame_id}/annotations": "annotations on one panorama",
    "/api/frames/{frame_id}/shares": "share links for one frame",
    # Fixed catalogues, and devices a deployment registers by hand.
    "/api/cameras/providers": "a fixed catalogue of adapter types",
    "/api/cameras": "camera devices registered by hand",
}


def test_every_growing_list_endpoint_is_paginated() -> None:
    """Read the schema, not the handlers: the response shape is the contract clients hold.

    A bare array has to be argued for in `BARE_ARRAY_IS_FINE` above. The default is a page,
    because "this list is short" is a statement about today's data.
    """
    from app.main import app

    unpaginated: list[str] = []
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            if method != "get":
                continue
            schema = (
                operation.get("responses", {})
                .get("200", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if schema.get("type") == "array" and path not in BARE_ARRAY_IS_FINE:
                unpaginated.append(f"{method.upper()} {path}")

    assert not unpaginated, (
        "These endpoints return a bare array. If the list is bounded by something other than how "
        "long the deployment has been running, add it to BARE_ARRAY_IS_FINE with the reason; "
        f"otherwise return Page[...]: {unpaginated}"
    )


@pytest.fixture
def floor_with_history(client: TestClient, admin, panorama_factory):
    """A floor, a capture point, and three visits to it."""
    from tests.integration.test_definition_of_done import _await_ready

    headers = auth_headers(client, admin.email)
    project = client.post(
        "/api/projects", json={"name": "Paging", "code": "PAGE-1"}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "B"}, headers=headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "L1", "level": 1},
        headers=headers,
    ).json()

    for visit in range(3):
        session_id = client.post(
            "/api/captures", json={"floor_id": floor["id"]}, headers=headers
        ).json()["id"]
        for index in range(2):
            client.post(
                f"/api/captures/{session_id}/upload",
                files={
                    "files": (
                        f"v{visit}_{index}.jpg",
                        panorama_factory((index * 1.2, 0.0, 0.0)),
                        "image/jpeg",
                    )
                },
                headers=headers,
            )
        client.post(f"/api/captures/{session_id}/finish", headers=headers)
        client.post(f"/api/captures/{session_id}/process", json={}, headers=headers)
        _await_ready(client, headers, session_id)
    return headers, floor["id"], session_id


def test_the_files_in_one_upload_come_back_as_a_page(floor_with_history, client) -> None:
    headers, _floor_id, session_id = floor_with_history

    response = client.get(f"/api/captures/{session_id}/files", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert body["limit"] == 50 and body["offset"] == 0
    # The frame count per file still arrives — it is now one grouped query for the page rather
    # than one query per row, and a refactor that dropped it would be invisible otherwise.
    assert all(item["frame_count"] == 1 for item in body["items"]), body["items"]


def test_a_second_page_of_files_is_reachable(floor_with_history, client) -> None:
    headers, _floor_id, session_id = floor_with_history

    first = client.get(
        f"/api/captures/{session_id}/files", headers=headers, params={"limit": 1}
    ).json()
    second = client.get(
        f"/api/captures/{session_id}/files", headers=headers, params={"limit": 1, "offset": 1}
    ).json()

    assert first["total"] == second["total"] == 2
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_the_paths_on_a_floor_come_back_newest_first(floor_with_history, client) -> None:
    """Newest first matters: the default page is what gets drawn on the plan."""
    headers, floor_id, _session_id = floor_with_history

    body = client.get(f"/api/floors/{floor_id}/paths", headers=headers).json()

    assert body["total"] == 3, body
    created = [item["id"] for item in body["items"]]
    assert created, "a floor with three processed captures should have paths"
    newest = client.get(
        f"/api/floors/{floor_id}/paths", headers=headers, params={"limit": 1}
    ).json()
    assert newest["items"][0]["id"] == created[0]
    assert newest["total"] == 3, "a short page must still report the whole count"
