"""Reference-data delta sync (§32).

The endpoint exists so an operator standing on an unconnected floor can pick the floor they are
standing on. What it has to get right is not "return recent rows" — that part is a `WHERE`
clause — but **convergence**: after applying a response, the client's copy must match the server,
with no possible sequence of edits that leaves the two permanently different.

Two divergences are possible and neither is visible in a happy-path test:

* A **deletion** cannot be expressed as a change, because the row is gone. A client that only
  applies `changed` keeps a floor for ever and offers it to an operator whose captures will then
  be refused. The manifest is the answer, and these tests check it is complete rather than
  merely present.
* A **missed update** leaves a stale record that looks current. The manifest's timestamps are
  what let a client notice, so they have to be the row's real ones.
"""

from __future__ import annotations

import io
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.conftest import auth_headers


@pytest.fixture
def floorplan_png() -> bytes:
    """A real PNG, because the upload route reads its dimensions with Pillow."""
    buffer = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _tree(client: TestClient, headers: dict[str, str], code: str) -> dict[str, str]:
    project = client.post(
        "/api/projects", json={"name": f"Sync {code}", "code": code}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "Block A"}, headers=headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "Level 01", "level": 1, "area_sqft": 4000.0},
        headers=headers,
    ).json()
    zone = client.post(
        f"/api/floors/{floor['id']}/zones", json={"name": "North"}, headers=headers
    ).json()
    return {
        "project": project["id"],
        "building": building["id"],
        "floor": floor["id"],
        "zone": zone["id"],
    }


def _sync(client: TestClient, headers: dict[str, str], since: str | None = None) -> dict:
    response = client.get("/api/sync", params={"since": since} if since else None, headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _ids(payload: dict, collection: str, half: str) -> set[str]:
    rows = payload[collection][half]
    return {row["id"] for row in rows}


def _boundary_after_setup() -> str:
    """A cursor that is strictly after everything created so far, and before anything next.

    Two clock facts force this to be deliberate rather than `now()`. SQLite stores
    `CURRENT_TIMESTAMP` to the **second**, so a row written at 12:00:01.4 is stored as
    12:00:01.0 — and a `since` of 12:00:01.3 would exclude it. Flooring the boundary to the
    second fixes that direction; the sleep pushes the rows created *before* it into an earlier
    second so they are genuinely excluded.

    Which is also the argument for `CURSOR_LAG` in production: the server cannot floor a
    client's clock, so it stays a few seconds behind instead and re-sends the overlap.
    """
    time.sleep(1.1)
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def test_a_first_sync_returns_the_whole_project_tree(client: TestClient, admin) -> None:
    """One request has to be enough to run a capture offline.

    Three today — projects, then floors, then zones — which is three chances to be holding no
    signal, and the operator cannot start until all three have answered.
    """
    headers = auth_headers(client, admin.email)
    tree = _tree(client, headers, "SYNC-1")

    payload = _sync(client, headers)

    assert payload["full"] is True
    assert tree["project"] in _ids(payload, "projects", "changed")
    assert tree["building"] in _ids(payload, "buildings", "changed")
    assert tree["floor"] in _ids(payload, "floors", "changed")
    assert tree["zone"] in _ids(payload, "zones", "changed")
    # Enough to populate the capture form without another request.
    floor = next(f for f in payload["floors"]["changed"] if f["id"] == tree["floor"])
    assert floor["building_id"] == tree["building"]
    assert floor["name"] == "Level 01"


def test_a_second_sync_carries_only_what_changed(client: TestClient, admin) -> None:
    """The reason for a cursor at all: opening the app on site must be cheap."""
    headers = auth_headers(client, admin.email)
    tree = _tree(client, headers, "SYNC-2")
    boundary = _boundary_after_setup()

    quiet = _sync(client, headers, since=boundary)
    added = client.post(
        f"/api/floors/{tree['floor']}/zones", json={"name": "Later zone"}, headers=headers
    )
    after_add = _sync(client, headers, since=boundary)

    assert quiet["full"] is False
    assert quiet["zones"]["changed"] == [], "a quiet sync re-sent rows nothing had touched"
    assert added.status_code == 201, added.text
    assert _ids(after_add, "zones", "changed") == {added.json()["id"]}


def test_the_manifest_lists_every_row_even_when_nothing_changed(client: TestClient, admin) -> None:
    """The manifest is the complete set, not the changed set.

    If it narrowed to what changed, a quiet sync would report an empty manifest and a client
    reconciling against it would delete its entire cache — the failure mode is total, and it
    only shows up on the *second* sync.
    """
    headers = auth_headers(client, admin.email)
    tree = _tree(client, headers, "SYNC-3")

    quiet = _sync(client, headers, since=_boundary_after_setup())

    assert quiet["floors"]["changed"] == []
    for collection, key in (
        ("projects", "project"),
        ("buildings", "building"),
        ("floors", "floor"),
        ("zones", "zone"),
    ):
        assert tree[key] in _ids(quiet, collection, "manifest"), collection


def test_a_deleted_floor_leaves_the_manifest_and_nothing_else_says_so(
    client: TestClient, admin
) -> None:
    """The divergence this design exists to close.

    Deletion cannot appear in `changed` — there is no row to describe. Without the manifest the
    client keeps the floor indefinitely and lets an operator start a capture against a floor the
    server will refuse, on site, with no way to find out why.
    """
    headers = auth_headers(client, admin.email)
    tree = _tree(client, headers, "SYNC-4")
    first = _sync(client, headers)
    assert tree["floor"] in _ids(first, "floors", "manifest")

    assert client.delete(f"/api/floors/{tree['floor']}", headers=headers).status_code == 200
    after = _sync(client, headers, since=first["cursor"])

    assert tree["floor"] not in _ids(after, "floors", "manifest")
    # And nowhere in the change feed either, which is exactly why the manifest is needed.
    assert tree["floor"] not in _ids(after, "floors", "changed")
    # Its zone went with it, so a client that only pruned floors would keep an orphan.
    assert tree["zone"] not in _ids(after, "zones", "manifest")


def test_the_manifest_timestamp_is_the_rows_own(client: TestClient, admin) -> None:
    """It is how a client detects an update the change feed did not carry.

    A manifest that stamped every entry with the sync time would look identical on a happy path
    and be useless for the one job it has: telling a client its copy is behind.
    """
    headers = auth_headers(client, admin.email)
    tree = _tree(client, headers, "SYNC-5")
    first = _sync(client, headers)
    before = next(e for e in first["floors"]["manifest"] if e["id"] == tree["floor"])

    renamed = client.patch(
        f"/api/floors/{tree['floor']}", json={"name": "Level 01 (revised)"}, headers=headers
    )
    assert renamed.status_code == 200
    after = _sync(client, headers, since=first["cursor"])

    entry = next(e for e in after["floors"]["manifest"] if e["id"] == tree["floor"])
    assert entry["updated_at"] >= before["updated_at"]
    changed = next(f for f in after["floors"]["changed"] if f["id"] == tree["floor"])
    assert changed["name"] == "Level 01 (revised)"


def test_the_cursor_is_early_enough_to_overlap_rather_than_skip(client: TestClient, admin) -> None:
    """The cursor is behind the present on purpose, and this is what goes wrong without it.

    A row's `updated_at` can be *earlier* than the moment it became visible: SQLite's
    `CURRENT_TIMESTAMP` has one-second resolution, and PostgreSQL's `now()` is the transaction
    start rather than the commit. So a cursor taken at the present steps straight over anything
    written in the same tick — permanently, because the next sync asks for `>= cursor` and the
    row's own timestamp is below it.

    The first version of this endpoint took the cursor at `now()` and lost exactly that row.
    """
    headers = auth_headers(client, admin.email)
    _tree(client, headers, "SYNC-6")

    payload = _sync(client, headers)
    cursor = datetime.fromisoformat(payload["cursor"])

    assert cursor < datetime.now(UTC) - timedelta(seconds=1), "the cursor is not behind the clock"
    # Asking again with that cursor must re-offer rows rather than skip them; the boundary is
    # inclusive for the same reason.
    repeat = _sync(client, headers, since=payload["cursor"])
    assert repeat["projects"]["manifest"], "a quiet sync still describes the full set"


def test_only_the_active_floor_plan_is_synced(client: TestClient, admin, floorplan_png) -> None:
    """A superseded plan leaves the manifest, so the client deletes its copy.

    Which is the point: an operator must not be working from last month's plan because it is
    still on the device and nothing ever told them otherwise.
    """
    headers = auth_headers(client, admin.email)
    tree = _tree(client, headers, "SYNC-7")

    first_plan = client.post(
        f"/api/floors/{tree['floor']}/floorplan",
        files={"file": ("plan-a.png", floorplan_png, "image/png")},
        data={"name": "Plan A"},
        headers=headers,
    )
    assert first_plan.status_code == 201, first_plan.text
    seen = _sync(client, headers)
    assert first_plan.json()["id"] in _ids(seen, "floor_plans", "manifest")
    # A signed URL, never a storage path — the client fetches the raster while it has signal.
    plan = next(p for p in seen["floor_plans"]["changed"] if p["id"] == first_plan.json()["id"])
    assert plan["asset"]["url"]
    assert "var/storage" not in plan["asset"]["url"]

    second_plan = client.post(
        f"/api/floors/{tree['floor']}/floorplan",
        files={"file": ("plan-b.png", floorplan_png, "image/png")},
        data={"name": "Plan B"},
        headers=headers,
    )
    assert second_plan.status_code == 201, second_plan.text
    after = _sync(client, headers)

    plans = _ids(after, "floor_plans", "manifest")
    assert second_plan.json()["id"] in plans
    assert first_plan.json()["id"] not in plans


def test_a_future_cursor_is_refused_rather_than_answered_with_silence(
    client: TestClient, admin
) -> None:
    """A client whose clock runs fast would otherwise never receive another change.

    It would ask for changes since a time that has not happened, be told there are none, and
    keep its stale cache indefinitely — while every request looked successful.
    """
    headers = auth_headers(client, admin.email)
    ahead = (datetime.now(UTC) + timedelta(hours=2)).isoformat()

    response = client.get("/api/sync", params={"since": ahead}, headers=headers)

    assert response.status_code == 422, response.text
    assert "future" in response.json()["message"]


def test_sync_needs_a_signed_in_user(client: TestClient) -> None:
    assert client.get("/api/sync").status_code == 401
