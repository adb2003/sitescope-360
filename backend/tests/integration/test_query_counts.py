"""The hot endpoints must not issue one query per row (§35, §40).

Two of them did, and neither is visible on a demo project — which is the whole difficulty. A
list that costs five queries per row is indistinguishable from a fast one until the data
arrives, and by then it is the landing page.

Measured before the fix:

* `GET /captures/{id}/frames` — 9 queries for one frame, 54 for ten, 104 for twenty. Four fixed
  plus five per frame, so the default page of fifty cost about 254. It backs the capture page
  and the viewer's timeline strip.
* `GET /dashboard` — 77 queries for six projects, 149 for twelve. Twelve per project, on the
  first screen after signing in.

These tests count statements rather than time, because a timing assertion on a developer laptop
is a flake and a statement count is the actual defect. The ceilings are deliberately loose: the
point is to catch a return to *per-row* growth, not to freeze an exact number that a legitimate
extra join would trip over.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.database.session import engine
from tests.conftest import auth_headers


class Counter:
    """Counts SQL statements issued while it is armed."""

    def __init__(self) -> None:
        self.count = 0
        self.armed = False

    def __enter__(self) -> Counter:
        self.count = 0
        self.armed = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.armed = False


@pytest.fixture
def sql() -> Iterator[Counter]:
    counter = Counter()

    def listen(*_args: object, **_kwargs: object) -> None:
        if counter.armed:
            counter.count += 1

    event.listen(engine, "before_cursor_execute", listen)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", listen)


@pytest.fixture
def capture_with_frames(client: TestClient, admin, panorama_factory):
    """One capture holding six frames, processed."""
    from tests.integration.test_definition_of_done import _await_ready

    headers = auth_headers(client, admin.email)
    project = client.post(
        "/api/projects", json={"name": "Counts", "code": "QCNT-1"}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "B"}, headers=headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "L1", "level": 1},
        headers=headers,
    ).json()
    session_id = client.post(
        "/api/captures", json={"floor_id": floor["id"]}, headers=headers
    ).json()["id"]
    for index in range(6):
        client.post(
            f"/api/captures/{session_id}/upload",
            files={
                "files": (
                    f"f{index}.jpg",
                    panorama_factory((index * 1.2, 0.0, 0.0)),
                    "image/jpeg",
                )
            },
            headers=headers,
        )
    client.post(f"/api/captures/{session_id}/finish", headers=headers)
    client.post(f"/api/captures/{session_id}/process", json={}, headers=headers)
    _await_ready(client, headers, session_id)
    return headers, session_id


def test_listing_frames_costs_the_same_for_two_as_for_six(
    client: TestClient, capture_with_frames, sql
) -> None:
    """The shape of the bug: a flat cost, not a small one.

    Asserting equality rather than a ceiling, because any per-row query shows up here as a
    difference however cheap it is. This caught a lazy load arriving *through* the serialiser —
    `CaptureFrameOut` has a `quality` field, so validating the ORM object re-queried it even
    after the batch fetch was added, and the count stayed at one per frame.
    """
    headers, session_id = capture_with_frames

    with sql as counter:
        client.get(f"/api/captures/{session_id}/frames", headers=headers, params={"limit": 2})
        two = counter.count
    with sql as counter:
        client.get(f"/api/captures/{session_id}/frames", headers=headers, params={"limit": 6})
        six = counter.count

    assert two == six, f"{two} queries for two frames, {six} for six — the cost is per row"
    assert six < 15, f"{six} queries for one page of frames"


def test_the_portfolio_costs_the_same_for_one_project_as_for_five(
    client: TestClient, admin, sql
) -> None:
    """The landing page. Twelve queries per project meant ~600 for a contractor with fifty."""
    headers = auth_headers(client, admin.email)
    first = client.post(
        "/api/projects", json={"name": "Folio 0", "code": "QFOL-0"}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{first['id']}/buildings", json={"name": "B"}, headers=headers
    ).json()
    client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "L1", "level": 1},
        headers=headers,
    )

    with sql as counter:
        client.get("/api/dashboard", headers=headers)
        one = counter.count

    for index in range(1, 5):
        more = client.post(
            "/api/projects",
            json={"name": f"Folio {index}", "code": f"QFOL-{index}"},
            headers=headers,
        ).json()
        block = client.post(
            f"/api/projects/{more['id']}/buildings", json={"name": "B"}, headers=headers
        ).json()
        client.post(
            f"/api/buildings/{block['id']}/floors",
            json={"name": "L1", "level": 1},
            headers=headers,
        )

    with sql as counter:
        body = client.get("/api/dashboard", headers=headers).json()
        five = counter.count

    assert body["total_projects"] == 5
    assert one == five, f"{one} queries for one project, {five} for five — the cost is per row"
    assert five < 20, f"{five} queries to render the landing page"


def test_the_portfolio_agrees_with_the_coverage_and_frequency_pages(
    client: TestClient, admin, panorama_factory
) -> None:
    """Two ways of computing a number is two numbers unless something holds them together.

    The portfolio aggregates coverage and frequency across every project at once; the project
    pages compute them one at a time. The due/overdue rule is shared outright, but the coverage
    ratio has to be aggregated differently to be grouped — so this asks both and compares.

    The shape of the project matters, and a simpler one would make this test worthless. The
    coverage page clamps each floor's visits to that floor's expectation before summing, so the
    grouped query is only *visibly* wrong when one floor is covered beyond its expectation while
    another is not covered at all — a finished floor and an untouched one, which is what a job
    in progress looks like. With a single floor, `_ratio`'s own cap hides the difference
    entirely, and an earlier version of this test passed against the bug for exactly that
    reason. On the demo data the real mistake read 0.82 where the coverage page said 0.42.
    """
    from app.database.session import SessionLocal
    from app.services import monitoring
    from tests.integration.test_definition_of_done import _await_ready

    headers = auth_headers(client, admin.email)
    project = client.post(
        "/api/projects", json={"name": "Agree", "code": "QAGR-1"}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "B"}, headers=headers
    ).json()

    def floor_with_zone(name: str, level: int, expected: int) -> str:
        floor = client.post(
            f"/api/buildings/{building['id']}/floors",
            json={"name": name, "level": level},
            headers=headers,
        ).json()
        client.post(
            f"/api/floors/{floor['id']}/zones",
            json={"name": f"Z{level}", "expected_capture_points": expected},
            headers=headers,
        )
        return str(floor["id"])

    # One floor finished beyond its plan, one not started. The clamp is the difference between
    # "that floor is done" and "the extra visits make the untouched floor look covered".
    done_floor = floor_with_zone("Done", 1, expected=2)
    floor_with_zone("Untouched", 2, expected=10)

    session_id = client.post(
        "/api/captures", json={"floor_id": done_floor}, headers=headers
    ).json()["id"]
    for index in range(3):
        client.post(
            f"/api/captures/{session_id}/upload",
            files={
                "files": (
                    f"a{index}.jpg",
                    panorama_factory((index * 1.2, 0.0, 0.0)),
                    "image/jpeg",
                )
            },
            headers=headers,
        )
    client.post(f"/api/captures/{session_id}/finish", headers=headers)
    client.post(f"/api/captures/{session_id}/process", json={}, headers=headers)
    _await_ready(client, headers, session_id)

    # Three visited points on a floor expecting two.
    frames = client.get(f"/api/captures/{session_id}/frames", headers=headers).json()["items"]
    for index, frame in enumerate(frames[:3]):
        placed = client.put(
            f"/api/frames/{frame['id']}/location",
            json={"x": 100.0 + index * 50.0, "y": 100.0},
            headers=headers,
        )
        assert placed.status_code == 200, placed.text

    row = next(
        item
        for item in client.get("/api/dashboard", headers=headers).json()["projects"]
        if item["project_id"] == project["id"]
    )
    db = SessionLocal()
    try:
        coverage = monitoring.compute_coverage(db, project["id"])
        frequency = monitoring.compute_frequency(db, project["id"])
    finally:
        db.close()

    # The project is genuinely part-covered, or the comparison below proves nothing: two
    # identical zeroes agree just as well as two correct numbers.
    assert 0.0 < coverage.coverage_ratio < 1.0, (
        f"the fixture must be partly covered to be worth comparing, got {coverage.coverage_ratio}"
    )
    assert row["coverage_ratio"] == pytest.approx(coverage.coverage_ratio), (
        f"portfolio says {row['coverage_ratio']}, the coverage page says {coverage.coverage_ratio}"
    )
    assert row["frequency_status"] == str(frequency.status)
    assert row["last_capture_at"] is not None


def test_opening_a_panorama_does_not_query_once_per_neighbour(
    client: TestClient, capture_with_frames, sql
) -> None:
    """The viewer manifest is the most-opened endpoint there is, and it was the worst offender.

    `_neighbours` read `item.location.code` per neighbour — six lazy loads on every open, the
    same N+1 already removed from the frame list and the portfolio, hiding inside an attribute
    access rather than in anything resembling a query.

    **The frames have to be placed for this to test anything.** The lazy load only fires for a
    neighbour that has a location, so against unplaced frames the bug and the fix cost exactly
    the same and an earlier version of this test passed with the bug restored. Placing them is
    the whole fixture.

    What a query count cannot check is the other half of the change: siblings are now selected
    as five columns rather than whole entities, which is the same single query either way. That
    one is visible in `viewer.viewer_manifest` and nowhere else, and it is stated there.
    """
    headers, session_id = capture_with_frames
    frames = client.get(
        f"/api/captures/{session_id}/frames", headers=headers, params={"limit": 6}
    ).json()["items"]
    assert len(frames) >= 4

    for index, frame in enumerate(frames):
        placed = client.put(
            f"/api/frames/{frame['id']}/location",
            json={"x": 100.0 + index * 40.0, "y": 120.0},
            headers=headers,
        )
        assert placed.status_code == 200, placed.text

    with sql as counter:
        response = client.get(f"/api/frames/{frames[0]['id']}/viewer", headers=headers)
        queries = counter.count

    assert response.status_code == 200, response.text
    neighbours = response.json()["neighbours"]
    # The neighbours must still be answered, and still carry their codes: a cheaper manifest
    # that stopped naming the places would pass a query count and fail the product.
    assert neighbours, "the viewer still needs somewhere to walk to"
    assert any(item["location_code"] for item in neighbours), neighbours
    assert queries < 14, (
        f"{queries} queries to open one panorama with {len(neighbours)} placed neighbours — "
        "one per neighbour is the shape to look for"
    )


def test_the_project_list_costs_the_same_for_one_project_as_for_six(
    client: TestClient, admin, sql
) -> None:
    """The sidebar, which loads on every sign-in and on every page.

    The last endpoint in this API that grew with its rows, and it hid behind the portfolio fix:
    that one rewrote `monitoring.build_portfolio` for `GET /dashboard`, while `GET /projects`
    went through a different helper counting one project at a time. Measured at seven queries per
    project — 10 for one, 45 for six, 87 for twelve — so around 360 for a contractor with fifty
    live jobs, before anything on the page was drawn.

    The counts themselves were checked a different way, against the development database, where
    the grouped version and the per-project version agreed on all seven figures for all six
    projects including the four with no captures at all. This test is only about the shape.
    """
    headers = auth_headers(client, admin.email)

    def add(index: int) -> None:
        project = client.post(
            "/api/projects",
            json={"name": f"List {index}", "code": f"QLST-{index}"},
            headers=headers,
        ).json()
        building = client.post(
            f"/api/projects/{project['id']}/buildings", json={"name": "B"}, headers=headers
        ).json()
        floor = client.post(
            f"/api/buildings/{building['id']}/floors",
            json={"name": "L1", "level": 1},
            headers=headers,
        ).json()
        # Rows under the floor as well: zones and capture points are counted through a join, so
        # a project with none of them would not exercise the grouping.
        client.post(f"/api/floors/{floor['id']}/zones", json={"name": "Z"}, headers=headers)
        client.post(
            f"/api/floors/{floor['id']}/locations",
            json={"code": f"P{index:02d}", "x": 0.5, "y": 0.5},
            headers=headers,
        )

    add(0)
    with sql as counter:
        client.get("/api/projects", headers=headers)
        one = counter.count

    for index in range(1, 6):
        add(index)

    with sql as counter:
        body = client.get("/api/projects", headers=headers).json()
        six = counter.count

    assert body["total"] == 6
    # Every row still carries its roll-ups; a fixed cost that returned zeros would pass a count
    # assertion and be useless.
    assert all(row["floor_count"] == 1 and row["zone_count"] == 1 for row in body["items"])
    assert all(row["location_count"] == 1 for row in body["items"])
    assert one == six, f"{one} queries for one project, {six} for six — the cost is per row"
    assert six < 15, f"{six} queries to render the project list"
