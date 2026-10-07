"""Deleting bytes has to actually delete them, and somebody has to be able to check (§44).

Two faults found together, and the second is why the first survived.

`delete_session_storage` built its prefix as ``build_key(...) + "/"`` and handed it to
`delete_prefix`, which validated it as a *key*. A trailing slash splits into a final empty
segment, so it raised — on every call, since the function was written. The call sat inside
``contextlib.suppress(Exception)``, so nothing was logged, nothing failed, and the result still
said ``{"swept_prefix": True, "objects": 0}``: indistinguishable from "swept it, there was
nothing there". A destructive step that quietly does nothing is worse than one that fails aloud.

It survived because nothing could ask the other question — *is there anything in storage that no
row points at?* Every deletion path is tested for what it removes; none could be tested for what
it left. The answer, measured on a development store, was 263 files and 23 MB.

So: prefixes are validated as prefixes, the sweep reports its own failure instead of swallowing
it, and `GET /api/system/storage/orphans` makes the question askable. That endpoint reports and
never deletes — an orphan costs storage, deleting the wrong object costs somebody their imagery,
and those are not symmetrical.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.storage import build_key, get_storage
from app.storage.base import validate_prefix
from tests.conftest import auth_headers


# ---- the validation that rejected its own callers ----------------------------
def test_a_prefix_may_end_in_a_slash() -> None:
    """The whole bug in one assertion: this raised, and the raise was suppressed."""
    key = build_key("projects", "abc", "floors", "def", "sessions", "CAP-1")

    assert validate_prefix(key) == key + "/"
    assert validate_prefix(key + "/") == key + "/"


def test_a_prefix_is_still_checked_for_traversal() -> None:
    """Accepting the slash must not accept everything else."""
    from app.core.errors import ValidationError

    for bad in ("../secrets/", "/etc/passwd", "projects/../../x/", "projects//x/"):
        with pytest.raises(ValidationError):
            validate_prefix(bad)


def test_the_prefix_sweep_actually_sweeps(storage_root) -> None:
    """Both spellings reach the same objects, which is what nothing checked before."""
    storage = get_storage()
    prefix = build_key("projects", "sweep-me", "floors", "f1", "sessions", "CAP-SWEEP")
    for name in ("a.jpg", "b.jpg"):
        storage.put_bytes(f"{prefix}/frames/{name}", b"x" * 32, "image/jpeg")

    assert storage.list_keys(prefix, 10)[0], "the fixture must put something there"
    removed = storage.delete_prefix(prefix + "/")

    assert removed == 2, f"the sweep removed {removed} objects"
    assert storage.list_keys(prefix, 10)[0] == []


# ---- the question that could not be asked ------------------------------------
@pytest.fixture
def project_with_a_plan(client: TestClient, admin):
    """A project holding one real stored object — an uploaded floor plan."""
    headers = auth_headers(client, admin.email)
    project = client.post(
        "/api/projects", json={"name": "Reconcile", "code": "RECON-1"}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "B"}, headers=headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "L1", "level": 1},
        headers=headers,
    ).json()
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), "white").save(buffer, format="PNG")
    uploaded = client.post(
        f"/api/floors/{floor['id']}/floorplan",
        files={"file": ("plan.png", buffer.getvalue(), "image/png")},
        data={"name": "GA"},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    return headers, project["id"], floor["id"]


def test_a_tidy_project_reports_no_orphans(client: TestClient, project_with_a_plan) -> None:
    """The baseline that makes the alarm mean something: zero when nothing has leaked."""
    headers, project_id, _floor_id = project_with_a_plan

    body = client.get(
        "/api/system/storage/orphans", headers=headers, params={"project_id": project_id}
    ).json()

    assert body["scanned"] > 0, "the fixture should have stored something to scan"
    assert body["orphaned"] == 0, body["sample"]
    assert body["referenced"] == body["scanned"]


def test_an_object_no_row_points_at_is_reported(client: TestClient, project_with_a_plan) -> None:
    headers, project_id, floor_id = project_with_a_plan
    stray = build_key("projects", project_id, "floors", floor_id, "sessions", "CAP-GONE", "x.jpg")
    get_storage().put_bytes(stray, b"y" * 128, "image/jpeg")

    body = client.get(
        "/api/system/storage/orphans", headers=headers, params={"project_id": project_id}
    ).json()

    assert body["orphaned"] == 1, body
    assert body["orphaned_bytes"] == 128
    assert stray in body["sample"]
    # It reports; it must not have removed anything.
    assert get_storage().exists(stray)


def test_the_report_says_when_it_stopped_early(client: TestClient, project_with_a_plan) -> None:
    """A truncated listing must say so rather than quietly understate the count."""
    headers, project_id, floor_id = project_with_a_plan
    for index in range(4):
        get_storage().put_bytes(
            build_key("projects", project_id, "floors", floor_id, "stray", f"{index}.jpg"),
            b"z",
            "image/jpeg",
        )

    body = client.get(
        "/api/system/storage/orphans",
        headers=headers,
        params={"project_id": project_id, "limit": 2},
    ).json()

    assert body["truncated"] is True
    assert body["scanned"] == 2


def test_another_projects_storage_is_not_reportable(
    client: TestClient, project_with_a_plan, engineer
) -> None:
    """The report names stored paths, so it answers to membership like everything else."""
    _headers, project_id, _floor_id = project_with_a_plan

    refused = client.get(
        "/api/system/storage/orphans",
        headers=auth_headers(client, engineer.email),
        params={"project_id": project_id},
    )

    assert refused.status_code in {403, 404}, refused.text
