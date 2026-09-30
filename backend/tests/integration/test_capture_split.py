"""One upload, several walkthroughs (§9, §33, §46).

An operator captures level 0, takes the lift, captures level 1, and uploads it all as one
capture. These tests put that upload through the real pipeline — the real ORB detector, the real
matcher, real rendered panoramas — and check that the platform notices, proposes the break with
its evidence, and splits on the operator's word into captures that can each be mapped and filed
on the floor they were actually walked.

The two floors are rendered with different texture seeds and the same geometry, which is what a
building actually looks like: identical structure, different surfaces. That gives the matcher
genuinely nothing to work with across the join, without the test having to assert on a number it
invented.

The test that earns its place most is `test_deleting_the_original_keeps_the_walkthroughs_imagery`.
Splitting re-points rows instead of copying hundreds of megabytes, so a walkthrough's frames live
under the storage prefix of the upload it came from — and the deletion that used to wipe that
prefix wholesale would take another capture's imagery with it. That is the price of the design
and the test is what holds it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth_headers
from tests.integration.test_definition_of_done import _await_ready

#: Five stops per floor, 1.2 m apart — far enough not to be rejected as near-duplicates, close
#: enough that consecutive frames of one walk match heavily.
STOPS = [(index * 1.2 - 2.4, 0.0, 0.0) for index in range(5)]

#: The texture seed stands in for "a different floor". Same room, different surfaces.
GROUND_SEED = 20260910
UPPER_SEED = 77712345


@pytest.fixture(scope="module")
def two_floors_of_panoramas() -> dict[int, list[bytes]]:
    """Rendered once for the module; rendering is the slowest thing in this file."""
    from app.processing.media import encode_jpeg
    from app.processing.synthetic import Room, render_panorama

    room = Room()
    return {
        seed: [
            encode_jpeg(render_panorama(room, stop, width=2048, height=1024, seed=seed), quality=88)
            for stop in STOPS
        ]
        for seed in (GROUND_SEED, UPPER_SEED)
    }


def _building(client: TestClient, headers: dict[str, str], code: str) -> tuple[str, str]:
    """A project with two floors, and the ids of both."""
    project = client.post(
        "/api/projects", json={"name": f"Split {code}", "code": code}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "Block A"}, headers=headers
    ).json()
    floors = [
        client.post(
            f"/api/buildings/{building['id']}/floors",
            json={"name": name, "level": level, "area_sqft": 5000.0},
            headers=headers,
        ).json()["id"]
        for name, level in (("Level 00", 0), ("Level 01", 1))
    ]
    return str(floors[0]), str(floors[1])


@pytest.fixture
def whole_morning(client: TestClient, admin, two_floors_of_panoramas):
    """One capture holding two floors' worth of walking, processed.

    This is the situation the feature exists for: the operator told the platform they were on
    level 0, because that is where they started, and then kept walking.
    """
    headers = auth_headers(client, admin.email)
    ground_id, upper_id = _building(client, headers, "SPL-1")

    session_id = client.post(
        "/api/captures",
        json={"floor_id": ground_id, "client_session_id": "CAP-20260929-080000-MORN"},
        headers=headers,
    ).json()["id"]

    for seed, label in ((GROUND_SEED, "g"), (UPPER_SEED, "u")):
        for index, payload in enumerate(two_floors_of_panoramas[seed]):
            response = client.post(
                f"/api/captures/{session_id}/upload",
                files={"files": (f"{label}{index}.jpg", payload, "image/jpeg")},
                headers=headers,
            )
            assert response.status_code in {200, 201}, response.text

    client.post(f"/api/captures/{session_id}/finish", headers=headers)
    client.post(f"/api/captures/{session_id}/process", json={}, headers=headers)
    _await_ready(client, headers, session_id)
    return headers, session_id, ground_id, upper_id


def _segments(client: TestClient, headers: dict[str, str], session_id: str) -> dict:
    response = client.get(f"/api/captures/{session_id}/segments", headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _plan(report: dict, upper_id: str) -> list[dict]:
    """Accept the proposal as-is, putting everything after the first walk on the upper floor."""
    return [
        {
            "start_sequence_index": segment["start_sequence_index"],
            "end_sequence_index": segment["end_sequence_index"],
            **({} if segment["index"] == 1 else {"floor_id": upper_id}),
        }
        for segment in report["segments"]
    ]


# ---- detection ---------------------------------------------------------------
def test_an_upload_that_crosses_floors_is_reported_as_two_walkthroughs(
    client: TestClient, whole_morning
) -> None:
    """The feature. Nobody asked; processing noticed."""
    headers, session_id, _ground, _upper = whole_morning

    report = _segments(client, headers, session_id)

    assert report["analysed"] is True
    assert len(report["segments"]) == 2, report["note"]
    assert [segment["frame_count"] for segment in report["segments"]] == [5, 5]
    assert "separate walkthroughs" in report["note"]


def test_the_break_names_the_frames_either_side_and_why(client: TestClient, whole_morning) -> None:
    """A proposal the operator cannot check is a proposal they cannot refuse."""
    headers, session_id, _ground, _upper = whole_morning

    proposed = [
        boundary
        for boundary in _segments(client, headers, session_id)["boundaries"]
        if boundary["proposed"]
    ]

    assert len(proposed) == 1
    boundary = proposed[0]
    assert boundary["scene_break"] is True
    # Not zero — two structurally identical spaces match each other by coincidence, which is
    # exactly why the bar is a fraction of what this walk's own links carry rather than a fixed
    # number. What matters is the contrast: a step shares hundreds, this shares a handful.
    assert boundary["inliers"] < 30, boundary["reason"]
    assert boundary["after_frame_id"] and boundary["before_frame_id"]
    frames = client.get(
        f"/api/captures/{session_id}/frames", headers=headers, params={"size": 50}
    ).json()["items"]
    ids = [frame["id"] for frame in frames]
    assert ids.index(boundary["before_frame_id"]) == ids.index(boundary["after_frame_id"]) + 1


def test_a_capture_that_has_not_been_processed_says_so_rather_than_guessing(
    client: TestClient, admin
) -> None:
    headers = auth_headers(client, admin.email)
    ground_id, _upper = _building(client, headers, "SPL-2")
    session_id = client.post("/api/captures", json={"floor_id": ground_id}, headers=headers).json()[
        "id"
    ]

    report = _segments(client, headers, session_id)

    assert report["analysed"] is False
    assert report["segments"] == []
    assert "process the capture first" in report["note"].lower()


# ---- splitting ---------------------------------------------------------------
def test_splitting_files_the_second_walk_on_the_floor_it_was_walked_on(
    client: TestClient, whole_morning
) -> None:
    headers, session_id, ground_id, upper_id = whole_morning
    report = _segments(client, headers, session_id)

    response = client.post(
        f"/api/captures/{session_id}/split",
        json={"segments": _plan(report, upper_id), "reprocess": False},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()

    assert result["moved_frames"] == 5
    (child,) = result["created"]
    assert child["floor_id"] == upper_id
    assert child["split_from_session_id"] == session_id
    assert child["split_segment_index"] == 2

    parent = client.get(f"/api/captures/{session_id}", headers=headers).json()
    assert parent["floor_id"] == ground_id
    assert parent["frame_count"] == 5
    assert client.get(f"/api/captures/{child['id']}/frames", headers=headers).json()["total"] == 5


def test_the_new_walkthrough_reads_like_any_other_capture(
    client: TestClient, whole_morning
) -> None:
    """Renumbered from zero, with its own file rows — not a view onto the original."""
    headers, session_id, _ground, upper_id = whole_morning
    report = _segments(client, headers, session_id)
    result = client.post(
        f"/api/captures/{session_id}/split",
        json={"segments": _plan(report, upper_id), "reprocess": False},
        headers=headers,
    ).json()
    child_id = result["created"][0]["id"]

    frames = client.get(f"/api/captures/{child_id}/frames", headers=headers).json()["items"]
    assert [frame["sequence_index"] for frame in frames] == [0, 1, 2, 3, 4]

    files = client.get(f"/api/captures/{child_id}/files", headers=headers).json()
    assert len(files) == 5, "each still it inherited needs a file row of its own"
    parent_files = client.get(f"/api/captures/{session_id}/files", headers=headers).json()
    assert {f["id"] for f in files}.isdisjoint({f["id"] for f in parent_files})


def test_the_original_stops_listing_the_files_whose_frames_left(
    client: TestClient, whole_morning
) -> None:
    """A file row with nothing behind it reads as a failed import, not a completed split.

    Every walkthrough needs its own row over each file it drew frames from, or deleting the
    original would cascade through that row and take the walkthrough's imagery. Where the whole
    file went, the original's row has no frames left and should go with them.
    """
    headers, session_id, _ground, upper_id = whole_morning
    report = _segments(client, headers, session_id)
    client.post(
        f"/api/captures/{session_id}/split",
        json={"segments": _plan(report, upper_id), "reprocess": False},
        headers=headers,
    )

    parent_files = client.get(f"/api/captures/{session_id}/files", headers=headers).json()

    assert [file["original_filename"] for file in parent_files] == [
        f"g{index}.jpg" for index in range(5)
    ]
    assert all(file["frame_count"] == 1 for file in parent_files), parent_files
    assert client.get(f"/api/captures/{session_id}", headers=headers).json()["file_count"] == 5


def test_the_track_that_ran_through_the_break_is_thrown_away(
    client: TestClient, whole_morning
) -> None:
    """A track estimated across a lift ride was not measuring anything."""
    headers, session_id, _ground, upper_id = whole_morning
    before = client.get(f"/api/captures/{session_id}", headers=headers).json()
    report = _segments(client, headers, session_id)

    client.post(
        f"/api/captures/{session_id}/split",
        json={"segments": _plan(report, upper_id), "reprocess": False},
        headers=headers,
    )

    after = client.get(f"/api/captures/{session_id}", headers=headers).json()
    assert before["mapping_confidence"] != "none" or before["mapping_note"]
    assert after["mapping_confidence"] == "none"
    assert after["mapping_method"] is None
    assert "split" in (after["mapping_note"] or "")
    assert after["grouping_report"] is None, "the proposal described a capture that is now gone"
    frames = client.get(f"/api/captures/{session_id}/frames", headers=headers).json()["items"]
    assert all(frame["rel_x"] is None for frame in frames)


def test_the_new_walkthrough_can_be_mapped_without_extracting_anything_again(
    client: TestClient, whole_morning
) -> None:
    """The per-frame stages really did run on these frames; only mapping has not."""
    headers, session_id, _ground, upper_id = whole_morning
    report = _segments(client, headers, session_id)
    child_id = client.post(
        f"/api/captures/{session_id}/split",
        json={"segments": _plan(report, upper_id), "reprocess": False},
        headers=headers,
    ).json()["created"][0]["id"]

    response = client.post(f"/api/captures/{child_id}/process", json={}, headers=headers)
    assert response.status_code in {200, 202}, response.text
    status = _await_ready(client, headers, child_id)

    stages = {stage["job_type"]: stage["status"] for stage in status["stages"]}
    assert stages["frame_extraction"] == "complete"
    assert stages["mapping"] == "complete"
    assert client.get(f"/api/captures/{child_id}/frames", headers=headers).json()["total"] == 5, (
        "re-extraction would have produced a second copy of every frame"
    )


# ---- the price of not copying bytes ------------------------------------------
def test_deleting_the_original_keeps_the_walkthroughs_imagery(
    client: TestClient, whole_morning, db
) -> None:
    """The split shares stored objects. Deletion has to know that.

    Before the guard, deleting the upload wiped its whole storage prefix — where the split
    walkthrough's frames still live — and the operator found out by opening a viewer onto
    nothing.
    """
    from app.models import CaptureFrame
    from app.storage import get_storage

    headers, session_id, _ground, upper_id = whole_morning
    report = _segments(client, headers, session_id)
    child_id = client.post(
        f"/api/captures/{session_id}/split",
        json={"segments": _plan(report, upper_id), "reprocess": False},
        headers=headers,
    ).json()["created"][0]["id"]

    keys = [
        frame.storage_key
        for frame in db.query(CaptureFrame).filter(CaptureFrame.session_id == child_id).all()
    ]
    assert keys and all(get_storage().exists(key) for key in keys)

    assert client.delete(f"/api/captures/{session_id}", headers=headers).status_code == 200

    assert client.get(f"/api/captures/{child_id}", headers=headers).status_code == 200
    missing = [key for key in keys if not get_storage().exists(key)]
    assert not missing, f"deleting the original destroyed the walkthrough's frames: {missing}"


# ---- refusals ----------------------------------------------------------------
def test_a_plan_that_leaves_frames_in_no_walkthrough_is_refused(
    client: TestClient, whole_morning
) -> None:
    headers, session_id, _ground, upper_id = whole_morning

    response = client.post(
        f"/api/captures/{session_id}/split",
        json={
            "segments": [
                {"start_sequence_index": 0, "end_sequence_index": 3},
                {"start_sequence_index": 5, "end_sequence_index": 9, "floor_id": upper_id},
            ]
        },
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert "belong to neither" in response.text


def test_a_plan_that_stops_short_of_the_last_frame_is_refused(
    client: TestClient, whole_morning
) -> None:
    headers, session_id, _ground, upper_id = whole_morning

    response = client.post(
        f"/api/captures/{session_id}/split",
        json={
            "segments": [
                {"start_sequence_index": 0, "end_sequence_index": 4},
                {"start_sequence_index": 5, "end_sequence_index": 7, "floor_id": upper_id},
            ]
        },
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert "last frame" in response.text


def test_the_first_walkthrough_cannot_be_moved_to_another_floor(
    client: TestClient, whole_morning
) -> None:
    """Its frames are stored under the capture's floor, and the split does not rewrite keys."""
    headers, session_id, _ground, upper_id = whole_morning

    response = client.post(
        f"/api/captures/{session_id}/split",
        json={
            "segments": [
                {"start_sequence_index": 0, "end_sequence_index": 4, "floor_id": upper_id},
                {"start_sequence_index": 5, "end_sequence_index": 9},
            ]
        },
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert "keeps its floor" in response.text


def test_a_floor_in_another_project_is_not_found(
    client: TestClient, whole_morning, engineer, admin_headers
) -> None:
    """A 404 and not a refusal: the answer must not confirm that the floor exists."""
    _headers, session_id, _ground, _upper = whole_morning
    other_ground, _other_upper = _building(client, admin_headers, "SPL-9")

    response = client.post(
        f"/api/captures/{session_id}/split",
        json={
            "segments": [
                {"start_sequence_index": 0, "end_sequence_index": 4},
                {"start_sequence_index": 5, "end_sequence_index": 9, "floor_id": other_ground},
            ]
        },
        headers=auth_headers(client, engineer.email),
    )

    assert response.status_code == 404, response.text


def test_a_capture_cannot_be_split_twice(client: TestClient, whole_morning) -> None:
    headers, session_id, _ground, upper_id = whole_morning
    report = _segments(client, headers, session_id)
    plan = _plan(report, upper_id)

    assert (
        client.post(
            f"/api/captures/{session_id}/split",
            json={"segments": plan, "reprocess": False},
            headers=headers,
        ).status_code
        == 200
    )
    again = client.post(
        f"/api/captures/{session_id}/split",
        json={"segments": plan, "reprocess": False},
        headers=headers,
    )

    assert again.status_code == 409, again.text
    assert "already been split" in again.text


def test_one_segment_is_not_a_split(client: TestClient, whole_morning) -> None:
    headers, session_id, _ground, _upper = whole_morning

    response = client.post(
        f"/api/captures/{session_id}/split",
        json={"segments": [{"start_sequence_index": 0, "end_sequence_index": 9}]},
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert "at least two" in response.text
