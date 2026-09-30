"""Choosing which sheet of a drawing set is the floor plan (§22).

A PDF sheet set is one file holding forty drawings, of which one or two are the plans anyone
wants to place capture points on. The upload route has always taken a page number and the
operator has always had to know it — on a set emailed by a consultant six months ago, that is a
guess followed by a re-upload.

The machinery to render another page was there from the start, and the source PDF is kept beside
every plan precisely so a second sheet could be pulled from it later. What was missing was a way
to *look* first.

The test that carries the most weight is `test_choosing_a_sheet_leaves_the_old_plans_points_alone`.
Capture locations are stored in the pixels of the plan they were placed on; another sheet is a
different drawing at a different scale, so re-rendering in place would move every placed point to
a coordinate that means nothing — silently, with nothing in the data recording it. A revision is
what stops that, and it is only worth anything if the old plan and its points survive intact.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import A3
from reportlab.pdfgen import canvas


def _set(*labels: str) -> bytes:
    """A real multi-sheet PDF: a title page, a plan, a section — like a set that arrives."""
    buffer = io.BytesIO()
    sheet = canvas.Canvas(buffer, pagesize=A3)
    for label in labels:
        sheet.setFont("Helvetica", 48)
        sheet.drawString(120, 200, label)
        sheet.rect(200, 300, 600, 400)
        sheet.showPage()
    sheet.save()
    return buffer.getvalue()


@pytest.fixture
def plan_from_a_set(client: TestClient, admin_headers: dict[str, str]):
    """A floor whose plan came from sheet 1 of a four-sheet set — the wrong sheet, as it happens."""
    project = client.post(
        "/api/projects", json={"name": "Sheets", "code": "SHEET-1"}, headers=admin_headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "Block A"}, headers=admin_headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "Level 01", "level": 1},
        headers=admin_headers,
    ).json()
    uploaded = client.post(
        f"/api/floors/{floor['id']}/floorplan",
        files={
            "file": (
                "set.pdf",
                _set("TITLE", "GA PLAN L01", "SECTION A-A", "DETAILS"),
                "application/pdf",
            )
        },
        data={"name": "Architect's set", "page": "1"},
        headers=admin_headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    return floor["id"], uploaded.json()


# ---- looking before choosing -------------------------------------------------
def test_the_set_lists_every_sheet_without_rendering_any(
    plan_from_a_set, client, admin_headers
) -> None:
    """The picker has to open instantly on a set with hundreds of pages."""
    _floor_id, plan = plan_from_a_set

    listed = client.get(f"/api/floorplans/{plan['id']}/sheets", headers=admin_headers)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["page_count"] == 4
    assert body["current_page"] == 1
    assert [sheet["page"] for sheet in body["sheets"]] == [1, 2, 3, 4]
    # Paper size is what lets an operator spot a plan sheet before any preview loads.
    assert all(sheet["orientation"] in {"portrait", "landscape"} for sheet in body["sheets"])


def test_a_sheet_can_be_previewed_as_an_image(plan_from_a_set, client, admin_headers) -> None:
    """The whole point: choose by looking, not by guessing a page number."""
    _floor_id, plan = plan_from_a_set

    preview = client.get(f"/api/floorplans/{plan['id']}/sheets/3/preview", headers=admin_headers)

    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"] == "image/png"
    assert preview.content[:8] == b"\x89PNG\r\n\x1a\n"
    # Small: a preview answers "plan or section", which survives being tiny.
    assert len(preview.content) < 100_000


def test_a_sheet_that_does_not_exist_is_refused(plan_from_a_set, client, admin_headers) -> None:
    _floor_id, plan = plan_from_a_set

    missing = client.get(f"/api/floorplans/{plan['id']}/sheets/99/preview", headers=admin_headers)

    assert missing.status_code == 422, missing.text


def test_an_uploaded_image_has_no_sheets_to_choose_between(
    client, admin_headers, panorama_factory
) -> None:
    """Saying so beats an empty list, which reads like a set with nothing in it."""
    project = client.post(
        "/api/projects", json={"name": "Raster", "code": "SHEET-2"}, headers=admin_headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "B"}, headers=admin_headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "L1", "level": 1},
        headers=admin_headers,
    ).json()
    plan = client.post(
        f"/api/floors/{floor['id']}/floorplan",
        files={"file": ("plan.jpg", panorama_factory(), "image/jpeg")},
        headers=admin_headers,
    ).json()

    listed = client.get(f"/api/floorplans/{plan['id']}/sheets", headers=admin_headers)

    assert listed.status_code == 422, listed.text
    assert "uploaded as an image" in listed.json()["message"]


# ---- choosing ----------------------------------------------------------------
def test_choosing_a_sheet_makes_it_the_active_plan(plan_from_a_set, client, admin_headers) -> None:
    floor_id, plan = plan_from_a_set

    chosen = client.post(f"/api/floorplans/{plan['id']}/sheets/2", headers=admin_headers)

    assert chosen.status_code == 200, chosen.text
    revision = chosen.json()["plan"]
    assert revision["id"] != plan["id"], "choosing a sheet must not overwrite the plan"
    assert revision["render"]["page"] == 2
    active = client.get(f"/api/floors/{floor_id}/floorplan", headers=admin_headers).json()
    assert active["id"] == revision["id"]


def test_choosing_a_sheet_leaves_the_old_plans_points_alone(
    plan_from_a_set, client, admin_headers
) -> None:
    """The reason this is a revision rather than a re-render.

    A capture location is stored in the pixels of the plan it was placed on. Another sheet is a
    different drawing at a different scale, so rewriting the raster underneath those points would
    relocate every one of them to a meaningless coordinate — and nothing in the data would say it
    had happened. The old plan keeps its pixels, so an earlier capture stays interpretable.
    """
    floor_id, plan = plan_from_a_set
    placed = client.post(
        f"/api/floors/{floor_id}/locations",
        json={"x": 120.0, "y": 240.0, "label": "Core"},
        headers=admin_headers,
    )
    assert placed.status_code == 201, placed.text
    location = placed.json()

    client.post(f"/api/floorplans/{plan['id']}/sheets/2", headers=admin_headers)

    after = client.get(f"/api/floors/{floor_id}/locations", headers=admin_headers).json()
    kept = next(row for row in after if row["id"] == location["id"])
    assert (kept["x"], kept["y"]) == (120.0, 240.0)
    assert kept["floor_plan_id"] == plan["id"], (
        "the point still belongs to the plan it was placed on"
    )


def test_the_old_revision_is_kept_and_deactivated(plan_from_a_set, client, admin_headers) -> None:
    floor_id, plan = plan_from_a_set

    client.post(f"/api/floorplans/{plan['id']}/sheets/3", headers=admin_headers)

    revisions = client.get(f"/api/floors/{floor_id}/floorplans", headers=admin_headers).json()
    by_id = {row["id"]: row for row in revisions}
    assert by_id[plan["id"]]["is_active"] is False
    assert len(revisions) == 2, "history is what makes an old capture's coordinates readable"


def test_a_chosen_sheet_starts_uncalibrated(plan_from_a_set, client, admin_headers) -> None:
    """A scale measured on one drawing is not the scale of another.

    Carrying the calibration across would produce a plan that looks calibrated and reports wrong
    distances, which is worse than one that admits it has no scale yet.
    """
    _floor_id, plan = plan_from_a_set
    client.put(
        f"/api/floorplans/{plan['id']}/calibration",
        json={"a": {"x": 0, "y": 0}, "b": {"x": 100, "y": 0}, "distance_m": 10.0},
        headers=admin_headers,
    )

    revision = client.post(f"/api/floorplans/{plan['id']}/sheets/2", headers=admin_headers).json()[
        "plan"
    ]

    assert revision["px_per_meter"] is None
    assert revision["calibration"] is None


def test_choosing_twice_does_not_stack_the_name(plan_from_a_set, client, admin_headers) -> None:
    """A name like "set — sheet 2 — sheet 3" is nobody's idea of a name."""
    _floor_id, plan = plan_from_a_set

    second = client.post(f"/api/floorplans/{plan['id']}/sheets/2", headers=admin_headers).json()[
        "plan"
    ]
    third = client.post(f"/api/floorplans/{second['id']}/sheets/3", headers=admin_headers).json()[
        "plan"
    ]

    assert third["name"] == "Architect's set — sheet 3"


def test_the_source_pdf_is_shared_not_copied(plan_from_a_set, client, admin_headers) -> None:
    """A set with four usable plans in it should not be stored four times."""
    _floor_id, plan = plan_from_a_set

    revision = client.post(f"/api/floorplans/{plan['id']}/sheets/2", headers=admin_headers).json()[
        "plan"
    ]

    assert revision["render"]["source_storage_key"] == plan["render"]["source_storage_key"]


def test_points_placed_on_an_earlier_sheet_are_counted_and_named(
    plan_from_a_set, client, admin_headers
) -> None:
    """The hazard this feature would otherwise make one click away.

    Switching sheets does not move a point — that is what the revision is for — but the
    floor-plan editor draws every point on the floor over whichever plan is active, so a point
    placed on sheet 1 appears on sheet 2 at a coordinate that describes the wrong drawing. It
    looks entirely plausible, which is what makes it worth saying out loud rather than leaving to
    be discovered.
    """
    floor_id, plan = plan_from_a_set
    for index in range(2):
        client.post(
            f"/api/floors/{floor_id}/locations",
            json={"x": 100.0 + index, "y": 200.0, "label": f"P{index}"},
            headers=admin_headers,
        )

    body = client.post(f"/api/floorplans/{plan['id']}/sheets/2", headers=admin_headers).json()

    assert body["points_from_earlier_sheets"] == 2
    assert "have not been moved" in body["note"]
    assert "re-place them here" in body["note"]


def test_a_floor_with_no_points_is_told_so_plainly(plan_from_a_set, client, admin_headers) -> None:
    """A warning that fires when there is nothing to warn about trains people to ignore it."""
    _floor_id, plan = plan_from_a_set

    body = client.post(f"/api/floorplans/{plan['id']}/sheets/2", headers=admin_headers).json()

    assert body["points_from_earlier_sheets"] == 0
    assert "No capture points" in body["note"]


def test_deleting_one_revision_leaves_the_set_its_siblings_still_need(
    plan_from_a_set, client, admin_headers
) -> None:
    """Sharing the PDF is the point; deleting it out from under a sibling is the price.

    A revision deletes the drawing set it was rendered from, because otherwise the file would
    outlive every row that names it. But since choosing a sheet *shares* that key rather than
    copying the file, deleting the superseded revision took the set with it — the remaining plan
    kept displaying its own raster and silently lost the ability to look at any other sheet, so
    nothing failed visibly at the moment the data went.
    """
    from app.storage import get_storage

    _floor_id, first = plan_from_a_set
    second = client.post(f"/api/floorplans/{first['id']}/sheets/2", headers=admin_headers).json()[
        "plan"
    ]
    source_key = first["render"]["source_storage_key"]
    assert second["render"]["source_storage_key"] == source_key

    deleted = client.delete(f"/api/floorplans/{first['id']}", headers=admin_headers)
    assert deleted.status_code == 200, deleted.text

    assert get_storage().exists(source_key), "the surviving revision's drawing set was deleted"
    sheets = client.get(f"/api/floorplans/{second['id']}/sheets", headers=admin_headers)
    assert sheets.status_code == 200, sheets.text
    assert sheets.json()["page_count"] == 4


def test_deleting_the_last_revision_does_take_the_set_with_it(
    plan_from_a_set, client, admin_headers
) -> None:
    """The guard must not turn into a leak: with no sibling left, the file still goes."""
    from app.storage import get_storage

    _floor_id, only = plan_from_a_set
    source_key = only["render"]["source_storage_key"]

    assert client.delete(f"/api/floorplans/{only['id']}", headers=admin_headers).status_code == 200

    assert not get_storage().exists(source_key)
