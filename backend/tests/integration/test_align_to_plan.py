"""Pinning an estimated track to the floor plan (§15).

A monocular track has a shape and nothing else — no scale, no bearing, no origin. Anchoring
supplies all three from a couple of points the operator places by hand, and then *the rest of
the walk places itself*. That leverage is the reason this exists and also the reason it is
dangerous: one bad anchor moves every frame, and the result still looks like a measurement.

So the tests below are mostly about refusals. The one that matters most is the residual check:
a fit that does not describe the same walk the anchors describe must be rejected outright rather
than applied with a caveat, because nothing downstream distinguishes a placed frame that was
trusted from one that was merely tolerated.
"""

from __future__ import annotations

import math

from fastapi.testclient import TestClient

from tests.conftest import auth_headers
from tests.integration.test_definition_of_done import _await_ready


def _floor_with_plan(client: TestClient, headers: dict[str, str], code: str = "ALN-1") -> str:
    project = client.post(
        "/api/projects", json={"name": "Align", "code": code}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "Block A"}, headers=headers
    ).json()
    return str(
        client.post(
            f"/api/buildings/{building['id']}/floors",
            json={"name": "Level 01", "level": 1, "area_sqft": 5000.0},
            headers=headers,
        ).json()["id"]
    )


def _walked_capture(
    client: TestClient, headers: dict[str, str], floor_id: str, panorama_factory, client_id: str
) -> tuple[str, list[dict]]:
    """A processed capture with a real estimated track: several frames from distinct positions."""
    session_id = client.post(
        "/api/captures",
        json={"floor_id": floor_id, "client_session_id": client_id},
        headers=headers,
    ).json()["id"]

    for index in range(4):
        client.post(
            f"/api/captures/{session_id}/upload",
            files={
                "files": (
                    f"P{index}.jpg",
                    panorama_factory((index * 1.2, 0.0, 0.0)),
                    "image/jpeg",
                )
            },
            headers=headers,
        )
    client.post(f"/api/captures/{session_id}/finish", headers=headers)
    client.post(f"/api/captures/{session_id}/process", json={}, headers=headers)
    _await_ready(client, headers, session_id)

    frames = client.get(f"/api/captures/{session_id}/frames", headers=headers).json()["items"]
    return session_id, frames


def _align(client: TestClient, headers: dict[str, str], session_id: str, anchors: list[dict]):
    return client.post(
        f"/api/captures/{session_id}/align", json={"anchors": anchors}, headers=headers
    )


def test_two_anchors_place_the_whole_track(client: TestClient, admin, panorama_factory) -> None:
    """The leverage the feature exists for: name two frames, the rest follow."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor_with_plan(client, headers)
    session_id, frames = _walked_capture(
        client, headers, floor_id, panorama_factory, "CAP-20260914-100000-AL01"
    )
    assert len(frames) >= 2, frames

    response = _align(
        client,
        headers,
        session_id,
        [
            {"frame_id": frames[0]["id"], "x": 100.0, "y": 100.0},
            {"frame_id": frames[-1]["id"], "x": 400.0, "y": 100.0},
        ],
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["placed_frames"] == len(frames)
    # Every frame now has a location, including the ones nobody touched.
    placed = client.get(f"/api/captures/{session_id}/frames", headers=headers).json()["items"]
    assert all(frame["location_id"] for frame in placed), placed


def test_an_anchored_frame_is_estimated_not_manual(
    client: TestClient, admin, panorama_factory
) -> None:
    """Nobody said where these frames were; a transform fitted to two points did (§46).

    The locations anchoring creates have always been recorded as `estimated`, and for a while the
    frames bound to them were stamped `manual` at confidence 1.0 — so the point said "a fit put me
    here" and the frame standing on it said "a person did", about the same placement. Only one of
    those can be true, and for every frame except the anchors themselves it is the first.
    """
    headers = auth_headers(client, admin.email)
    floor_id = _floor_with_plan(client, headers, "ALN-PROV")
    session_id, frames = _walked_capture(
        client, headers, floor_id, panorama_factory, "CAP-20260914-100000-ALPV"
    )

    _align(
        client,
        headers,
        session_id,
        [
            {"frame_id": frames[0]["id"], "x": 100.0, "y": 100.0},
            {"frame_id": frames[-1]["id"], "x": 400.0, "y": 100.0},
        ],
    )

    placed = client.get(f"/api/captures/{session_id}/frames", headers=headers).json()["items"]
    assert {frame["position_provenance"] for frame in placed} == {"estimated"}
    # Confidence now comes from the fit rather than being stamped 1.0 — but that is deliberately
    # not asserted here. This walk maps at full confidence, so the estimator's own number *is*
    # 1.0 and the two sources are indistinguishable on this input. An assertion that cannot fail
    # for the right reason is worse than none, and the provenance above is the claim that matters.


def test_the_anchors_land_where_they_were_dropped(
    client: TestClient, admin, panorama_factory
) -> None:
    """A transform that does not honour its own anchors has fitted something else.

    This is the check that the maths is wired up the right way round: the fit is *defined* by
    these two correspondences, so they are the two points whose final positions are predictable
    regardless of what the estimator recovered in between.
    """
    headers = auth_headers(client, admin.email)
    floor_id = _floor_with_plan(client, headers)
    session_id, frames = _walked_capture(
        client, headers, floor_id, panorama_factory, "CAP-20260914-100100-AL02"
    )

    first, last = frames[0], frames[-1]
    response = _align(
        client,
        headers,
        session_id,
        [
            {"frame_id": first["id"], "x": 120.0, "y": 240.0},
            {"frame_id": last["id"], "x": 520.0, "y": 240.0},
        ],
    )
    assert response.status_code == 200, response.text

    locations = {
        item["id"]: item
        for item in client.get(f"/api/floors/{floor_id}/locations", headers=headers).json()
    }
    placed = {
        frame["id"]: locations[frame["location_id"]]
        for frame in client.get(f"/api/captures/{session_id}/frames", headers=headers).json()[
            "items"
        ]
    }

    # Within the residual the fit itself reported, not to the pixel: two anchors over a track
    # with estimation noise will not land exactly, and pretending otherwise would make this test
    # a measurement of the estimator rather than of the transform.
    tolerance = max(response.json()["rms_error_px"] * 2, 1.0)
    assert (
        math.hypot(placed[first["id"]]["x"] - 120.0, placed[first["id"]]["y"] - 240.0) <= tolerance
    )
    assert math.hypot(placed[last["id"]]["x"] - 520.0, placed[last["id"]]["y"] - 240.0) <= tolerance


def test_one_anchor_is_refused_with_the_reason(client: TestClient, admin, panorama_factory) -> None:
    """One point fixes the origin and leaves rotation and scale free — not merely imprecise."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor_with_plan(client, headers)
    session_id, frames = _walked_capture(
        client, headers, floor_id, panorama_factory, "CAP-20260914-100200-AL03"
    )

    refused = _align(
        client, headers, session_id, [{"frame_id": frames[0]["id"], "x": 1.0, "y": 1.0}]
    )

    assert refused.status_code == 422
    assert "anchor" in refused.text.lower()


def test_two_anchors_on_the_same_frame_are_refused(
    client: TestClient, admin, panorama_factory
) -> None:
    """Coincident source points leave scale undetermined however far apart the targets are."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor_with_plan(client, headers)
    session_id, frames = _walked_capture(
        client, headers, floor_id, panorama_factory, "CAP-20260914-100300-AL04"
    )

    refused = _align(
        client,
        headers,
        session_id,
        [
            {"frame_id": frames[0]["id"], "x": 10.0, "y": 10.0},
            {"frame_id": frames[0]["id"], "x": 400.0, "y": 400.0},
        ],
    )

    assert refused.status_code == 422
    assert "collinear or coincident" in refused.text.lower()


def test_a_frame_from_another_capture_is_refused(
    client: TestClient, admin, panorama_factory
) -> None:
    """Anchoring to a frame that is not on this track would fit a transform to nothing."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor_with_plan(client, headers)
    mine, my_frames = _walked_capture(
        client, headers, floor_id, panorama_factory, "CAP-20260914-100400-AL05"
    )
    _, other_frames = _walked_capture(
        client, headers, floor_id, panorama_factory, "CAP-20260914-100500-AL06"
    )

    refused = _align(
        client,
        headers,
        mine,
        [
            {"frame_id": my_frames[0]["id"], "x": 10.0, "y": 10.0},
            {"frame_id": other_frames[0]["id"], "x": 400.0, "y": 400.0},
        ],
    )

    assert refused.status_code == 422
    assert "not part of this capture" in refused.text.lower()


def test_a_capture_with_no_estimated_track_is_refused(client: TestClient, admin) -> None:
    """Unprocessed frames have no rel_x/rel_y, so there is no shape to transform."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor_with_plan(client, headers)
    session_id = client.post(
        "/api/captures",
        json={"floor_id": floor_id, "client_session_id": "CAP-20260914-100600-AL07"},
        headers=headers,
    ).json()["id"]

    refused = _align(
        client,
        headers,
        session_id,
        [{"frame_id": "a", "x": 1.0, "y": 1.0}, {"frame_id": "b", "x": 2.0, "y": 2.0}],
    )

    assert refused.status_code == 422
    assert "frames" in refused.text.lower()


def test_placed_positions_are_marked_estimated_not_measured(
    client: TestClient, admin, panorama_factory
) -> None:
    """§46. These came out of a transform; presenting them as measured would be the lie."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor_with_plan(client, headers)
    session_id, frames = _walked_capture(
        client, headers, floor_id, panorama_factory, "CAP-20260914-100700-AL08"
    )

    _align(
        client,
        headers,
        session_id,
        [
            {"frame_id": frames[0]["id"], "x": 100.0, "y": 100.0},
            {"frame_id": frames[-1]["id"], "x": 400.0, "y": 100.0},
        ],
    )

    locations = client.get(f"/api/floors/{floor_id}/locations", headers=headers).json()
    assert locations, "nothing was placed"
    assert all(item["provenance"] == "estimated" for item in locations), locations
