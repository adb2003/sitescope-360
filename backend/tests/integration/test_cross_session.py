"""Placing a capture by recognising a floor that has been captured before (§15, §46).

The first walk of a floor is anchored by hand. Every walk after it should not have to be, and
that is what these tests are about — real panoramas, the real ORB detector, the real pipeline,
and the assertion that the second capture of a floor arrives already placed.

The measurement that matters is not "were frames placed" but **were they placed on the earlier
capture's own points**. Comparing a place across dates is the product's central feature and it
works by two frames sharing a location row; a second capture that lands two pixels away from the
first has been placed and still compares against nothing. So the tests check location identity,
not coordinates.

Real imagery on purpose, unlike `tests/vision/test_relocalize.py`. The thresholds there are
checked against synthesised geometry so they mean the same thing on every platform; what cannot
be checked that way is whether ORB, on actual panoramas of an actual room, produces enough
verified inliers at a realistic revisit distance for any of it to fire.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth_headers
from tests.integration.test_definition_of_done import _await_ready

#: Where the operator walks. Five stops in a line, 1.2 m apart.
WALK = [(index * 1.2, 0.0, 0.0) for index in range(5)]

#: The second visit, a week later: the same route, but nobody stands where they stood before.
#: 15 cm off is both realistic and well inside the viewpoint tolerance; it also guarantees the
#: uploaded bytes differ, so the second capture is not silently de-duplicated against the first.
REVISIT = [(x + 0.15, y, heading) for x, y, heading in WALK]

#: Plan pixels the first capture is anchored to. 100px per 1.2 m stop, so the whole walk spans
#: 400px — comfortably past `MIN_ANCHOR_SEPARATION_PX`.
ANCHOR_START = (100.0, 100.0)
ANCHOR_END = (500.0, 100.0)


def _floor(client: TestClient, headers: dict[str, str], code: str) -> str:
    project = client.post(
        "/api/projects", json={"name": f"Cross {code}", "code": code}, headers=headers
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


def _capture(
    client: TestClient,
    headers: dict[str, str],
    floor_id: str,
    panorama_factory,
    client_id: str,
    positions: list[tuple[float, float, float]],
) -> tuple[str, list[dict]]:
    """Upload a walk, run the whole pipeline, and return the session and its frames."""
    session_id = client.post(
        "/api/captures",
        json={"floor_id": floor_id, "client_session_id": client_id},
        headers=headers,
    ).json()["id"]

    for index, position in enumerate(positions):
        response = client.post(
            f"/api/captures/{session_id}/upload",
            files={"files": (f"P{index}.jpg", panorama_factory(position), "image/jpeg")},
            headers=headers,
        )
        assert response.status_code in {200, 201}, response.text

    client.post(f"/api/captures/{session_id}/finish", headers=headers)
    client.post(f"/api/captures/{session_id}/process", json={}, headers=headers)
    _await_ready(client, headers, session_id)

    frames = client.get(f"/api/captures/{session_id}/frames", headers=headers).json()["items"]
    return session_id, frames


def _anchored_first_capture(
    client: TestClient, headers: dict[str, str], floor_id: str, panorama_factory, code: str
) -> tuple[str, list[dict]]:
    """The floor's first walk, placed by hand — the reference everything else is matched to."""
    session_id, frames = _capture(
        client, headers, floor_id, panorama_factory, f"CAP-20260901-090000-{code}", WALK
    )
    response = client.post(
        f"/api/captures/{session_id}/align",
        json={
            "anchors": [
                {"frame_id": frames[0]["id"], "x": ANCHOR_START[0], "y": ANCHOR_START[1]},
                {"frame_id": frames[-1]["id"], "x": ANCHOR_END[0], "y": ANCHOR_END[1]},
            ]
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    placed = client.get(f"/api/captures/{session_id}/frames", headers=headers).json()["items"]
    return session_id, placed


def _located(frames: list[dict]) -> list[str]:
    return [frame["location_id"] for frame in frames if frame.get("location_id")]


@pytest.fixture
def walked_floor(client: TestClient, admin, panorama_factory):
    """A floor whose first capture is processed and anchored, ready to be recognised against."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers, "XS-1")
    first_id, first_frames = _anchored_first_capture(
        client, headers, floor_id, panorama_factory, "F1"
    )
    assert len(_located(first_frames)) >= 3, "the reference capture must be placed"
    return headers, floor_id, first_id, first_frames


# ---- the capability ----------------------------------------------------------
def test_a_second_capture_of_the_same_floor_places_itself(
    client: TestClient, panorama_factory, walked_floor
) -> None:
    """The feature. Nobody anchored this one, and it arrived on the plan anyway."""
    headers, floor_id, _first_id, _first_frames = walked_floor

    _second_id, second_frames = _capture(
        client, headers, floor_id, panorama_factory, "CAP-20260908-090000-F2", REVISIT
    )

    placed = _located(second_frames)
    assert len(placed) >= 3, f"expected the revisit to be placed automatically, got {placed}"


def test_a_recognised_frame_joins_the_earlier_captures_point(
    client: TestClient, panorama_factory, walked_floor
) -> None:
    """Placement is not the goal; *comparability* is.

    Two dates at one place is the product's central view, and it is built on two frames sharing a
    location row. A second capture placed near the first — but on points of its own — looks
    correct on the plan and compares against nothing.
    """
    headers, floor_id, _first_id, first_frames = walked_floor

    _second_id, second_frames = _capture(
        client, headers, floor_id, panorama_factory, "CAP-20260908-090000-F3", REVISIT
    )

    shared = set(_located(first_frames)) & set(_located(second_frames))
    assert len(shared) >= 3, "recognised frames should reuse the earlier capture's locations"


def test_how_the_capture_was_placed_is_recorded(
    client: TestClient, panorama_factory, walked_floor
) -> None:
    """A hand-anchored placement and a recognised one look identical in the coordinates (§46)."""
    headers, floor_id, first_id, _first_frames = walked_floor

    second_id, _second_frames = _capture(
        client, headers, floor_id, panorama_factory, "CAP-20260908-090000-F4", REVISIT
    )

    first = client.get(f"/api/captures/{first_id}", headers=headers).json()
    second = client.get(f"/api/captures/{second_id}", headers=headers).json()
    assert first["placement_method"] == "manual_anchors"
    assert second["placement_method"] == "cross_session"


def test_the_evidence_behind_an_automatic_placement_is_kept(
    client: TestClient, panorama_factory, walked_floor
) -> None:
    """An automatic placement has to be arguable, not merely trusted."""
    headers, floor_id, _first_id, _first_frames = walked_floor

    second_id, _frames = _capture(
        client, headers, floor_id, panorama_factory, "CAP-20260908-090000-F5", REVISIT
    )

    report = client.get(f"/api/captures/{second_id}", headers=headers).json()["localization_report"]
    assert report["mode"] in {"transform", "recognised"}
    assert report["reason"] == "placed"
    assert report["recognition"]["matched_frames"] >= 3
    for match in report["recognition"]["matches"]:
        assert match["parallax_deg"] < 8.0, match
        assert match["inlier_count"] >= 90, match


# ---- refusals ----------------------------------------------------------------
def test_the_first_capture_of_a_floor_has_nothing_to_recognise(
    client: TestClient, admin, panorama_factory
) -> None:
    """Every floor has a first capture, and it must not look like a failure."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers, "XS-2")

    session_id, frames = _capture(
        client, headers, floor_id, panorama_factory, "CAP-20260901-090000-F6", WALK
    )

    assert _located(frames) == []
    report = client.get(f"/api/captures/{session_id}", headers=headers).json()[
        "localization_report"
    ]
    assert report["mode"] == "none"
    # The token, not the prose: a caller that branches on wording breaks when the wording
    # improves, and "never captured before" needs a different response from "not recognised".
    assert report["reason"] == "no_references"
    assert report["references_considered"] == 0
    assert "anchor it by hand once" in report["note"].lower()


def test_another_floors_captures_are_never_used_as_references(
    client: TestClient, admin, panorama_factory, walked_floor
) -> None:
    """Plan pixels are per-floor: (400, 300) is a different room on every level."""
    headers, _floor_id, _first_id, _first_frames = walked_floor
    other_floor = _floor(client, headers, "XS-3")

    session_id, frames = _capture(
        client, headers, other_floor, panorama_factory, "CAP-20260908-090000-F7", REVISIT
    )

    assert _located(frames) == []
    report = client.get(f"/api/captures/{session_id}", headers=headers).json()[
        "localization_report"
    ]
    assert report["references_considered"] == 0


def test_an_already_placed_capture_is_not_quietly_re_placed(
    client: TestClient, panorama_factory, walked_floor
) -> None:
    """Re-running must not move frames a person may have corrected by hand."""
    headers, floor_id, _first_id, _first_frames = walked_floor
    second_id, _frames = _capture(
        client, headers, floor_id, panorama_factory, "CAP-20260908-090000-F8", REVISIT
    )

    response = client.post(f"/api/captures/{second_id}/localize", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["reason"] == "already_placed"
    assert body["placed_frames"] == 0
    assert "already placed" in body["note"]


def test_the_endpoint_places_a_capture_that_processing_could_not(
    client: TestClient, admin, panorama_factory
) -> None:
    """Why the endpoint exists at all.

    A capture processed *before* its floor had any reference has nothing to match against. Once
    the floor's first walk is anchored, re-running recognition places it — without reprocessing
    the imagery, which is the expensive half.
    """
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers, "XS-4")

    orphan_id, orphan_frames = _capture(
        client, headers, floor_id, panorama_factory, "CAP-20260901-080000-F9", REVISIT
    )
    assert _located(orphan_frames) == [], "nothing to match against yet"

    _anchored_first_capture(client, headers, floor_id, panorama_factory, "FA")

    response = client.post(f"/api/captures/{orphan_id}/localize", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    assert body["placed_frames"] >= 3, body
    assert body["reused_locations"] >= 3, body
