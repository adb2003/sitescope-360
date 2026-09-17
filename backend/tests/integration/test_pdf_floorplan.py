"""Rasterising a PDF floor plan (§12).

The format contractors actually hand over used to be the one format the product could not use: a
PDF uploaded fine, recorded `width_px=0`, and then told the operator to go and export a PNG by
hand. Every positional feature works in plan *pixels*, so that plan supported none of them.

Two things here matter more than the happy path.

**The pixel grid is a coordinate system.** A capture location stores x/y against a plan's pixels,
so if the same PDF rendered to a different size on a different machine or after an upgrade, every
stored coordinate on that plan would quietly mean somewhere else. That is unrecoverable — there
is no record of what the old grid was — so the size has to come only from the page's own geometry
and an explicit DPI.

**Scale must not be invented.** A PDF knows its paper size and nothing about the building; the
drawing scale is printed on the sheet, not stored in the file. Deriving `px_per_meter` from page
geometry would produce a confident and completely wrong number, which is the §46 failure mode.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from reportlab.lib.pagesizes import A1, A3
from reportlab.pdfgen import canvas

from app.processing.pdf import (
    DEFAULT_DPI,
    MAX_EDGE_PX,
    POINTS_PER_INCH,
    count_pages,
    effective_dpi,
    looks_like_pdf,
    render_page,
)
from app.storage import get_storage
from tests.conftest import auth_headers


def _pdf(*labels: str, pagesize=A3) -> bytes:
    """A real multi-page PDF, drawn with reportlab — already a dependency for reports."""
    buffer = io.BytesIO()
    sheet = canvas.Canvas(buffer, pagesize=pagesize)
    for label in labels:
        sheet.setFont("Helvetica", 48)
        sheet.drawString(120, 200, label)
        sheet.rect(200, 300, 600, 400)
        sheet.showPage()
    sheet.save()
    return buffer.getvalue()


def _floor(client: TestClient, headers: dict[str, str], code: str) -> str:
    project = client.post(
        "/api/projects", json={"name": "PDF plans", "code": code}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "Block A"}, headers=headers
    ).json()
    return str(
        client.post(
            f"/api/buildings/{building['id']}/floors",
            json={"name": "Level 01", "level": 1, "area_sqft": 4000.0},
            headers=headers,
        ).json()["id"]
    )


def _upload(client: TestClient, headers: dict[str, str], floor_id: str, data: bytes, **form):
    return client.post(
        f"/api/floors/{floor_id}/floorplan",
        files={"file": ("sheets.pdf", data, "application/pdf")},
        data={"name": "Sheet set", **form},
        headers=headers,
    )


# ---- the rasteriser ---------------------------------------------------------------------------
def test_a_pdf_is_recognised_by_its_magic_bytes() -> None:
    assert looks_like_pdf(_pdf("L01"))
    assert not looks_like_pdf(b"\x89PNG\r\n\x1a\n")
    assert not looks_like_pdf(b"")


def test_the_page_count_is_read_without_rendering() -> None:
    assert count_pages(_pdf("L01", "L02", "L03")) == 3


def test_the_pixel_size_follows_the_page_geometry_and_the_dpi() -> None:
    """The coordinate system, derived rather than guessed.

    A3 is 297x420mm, so at 150 DPI the long edge is 420/25.4*150 ≈ 2480px. Asserting the
    arithmetic rather than a remembered number is what would catch a change of default or a
    unit slip.
    """
    rendered = render_page(_pdf("L01"), dpi=DEFAULT_DPI)

    expected_long = round(max(rendered.page_width_pt, rendered.page_height_pt) / 72 * DEFAULT_DPI)
    assert abs(max(rendered.width_px, rendered.height_px) - expected_long) <= 1
    assert rendered.dpi == DEFAULT_DPI


def test_the_same_pdf_renders_to_the_same_size_every_time() -> None:
    """Capture coordinates are stored against these pixels.

    If this drifted, every placement on every PDF-sourced plan would silently point somewhere
    else, with no record of the grid they were made against to recover from.
    """
    data = _pdf("L01")

    first = render_page(data)
    second = render_page(data)

    assert (first.width_px, first.height_px) == (second.width_px, second.height_px)
    assert first.dpi == second.dpi


def test_the_output_is_a_png_of_the_reported_size() -> None:
    rendered = render_page(_pdf("L01"))

    with Image.open(io.BytesIO(rendered.png)) as image:
        assert image.format == "PNG"
        assert image.size == (rendered.width_px, rendered.height_px)
        # RGB, not RGBA: a drawing renders fully opaque, and the alpha channel would add a
        # quarter to every stored plan for nothing.
        assert image.mode == "RGB"


def test_a_requested_page_is_the_one_rendered() -> None:
    """A sheet set is not one plan, and the wrong sheet is worse than no sheet.

    Rendering page 1 regardless would place capture points from Level 02 onto Level 01's
    drawing — plausible-looking, and wrong in a way nobody would notice from the coordinates.
    """
    data = _pdf("LEVEL 01", "LEVEL 02")

    first = render_page(data, page=1)
    second = render_page(data, page=2)

    assert first.page == 1
    assert second.page == 2
    assert first.page_count == second.page_count == 2
    # Different drawings, so different bytes. Equality here would mean the page index was ignored.
    assert first.png != second.png


def test_asking_for_a_page_that_does_not_exist_says_how_many_there_are() -> None:
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError, match="1 page"):
        render_page(_pdf("only one"), page=2)


def test_a_huge_sheet_is_scaled_down_and_says_what_dpi_it_used() -> None:
    """A browser decodes a raster to uncompressed RGBA to draw the minimap.

    Refusing the upload would leave the operator with no plan at all, so the DPI drops instead —
    and the value actually used is recorded, because a plan that silently rendered at a different
    resolution than the default is a plan whose metadata lies.
    """
    # 4x A1 in each direction: far past the edge cap at the default DPI.
    huge = (A1[0] * 4, A1[1] * 4)
    rendered = render_page(_pdf("HUGE", pagesize=huge))

    assert max(rendered.width_px, rendered.height_px) <= MAX_EDGE_PX
    assert rendered.dpi < DEFAULT_DPI
    assert rendered.to_metadata()["dpi"] == round(rendered.dpi, 2)


def test_the_dpi_ceiling_is_only_a_ceiling() -> None:
    # A small sheet keeps the requested DPI; only an oversized one is reduced.
    long_edge_pt = MAX_EDGE_PX * POINTS_PER_INCH / DEFAULT_DPI

    assert effective_dpi(100.0, long_edge_pt / 2, DEFAULT_DPI) == DEFAULT_DPI
    assert effective_dpi(100.0, long_edge_pt * 2, DEFAULT_DPI) == pytest.approx(DEFAULT_DPI / 2)


def test_a_damaged_pdf_is_refused_with_a_reason() -> None:
    from app.core.errors import UnsupportedMediaError

    with pytest.raises(UnsupportedMediaError):
        render_page(b"%PDF-1.7\nthis is not actually a pdf body")


def test_the_metadata_records_the_page_and_refuses_to_imply_a_scale() -> None:
    """The §46 line: precise about paper, silent about the building.

    The page size is real and recorded. The drawing scale is a convention printed on the sheet,
    so it stays a human's to enter — and the record says so rather than leaving the next reader
    to assume the numbers imply a scale.
    """
    metadata = render_page(_pdf("L01", "L02"), page=2).to_metadata()

    assert metadata["source"] == "pdf"
    assert metadata["page"] == 2
    assert metadata["page_count"] == 2
    assert metadata["page_width_pt"] and metadata["page_height_pt"]
    # Nothing here may look like a real-world scale, however tempting the page size makes it.
    assert "px_per_meter" not in metadata
    assert "scale" not in {key for key in metadata if key != "scale_note"}
    assert "must still be calibrated" in str(metadata["scale_note"])


# ---- the route --------------------------------------------------------------------------------
def test_uploading_a_pdf_produces_a_placeable_plan(client: TestClient, admin) -> None:
    """The whole point. This used to return width_px=0 and a note telling the operator to leave."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers, "PDF-1")

    response = _upload(client, headers, floor_id, _pdf("LEVEL 01"))

    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan["width_px"] > 0 and plan["height_px"] > 0
    assert plan["content_type"] == "image/png"
    assert plan["render"]["source"] == "pdf"
    assert plan["render"]["page"] == 1
    # And the stored object really is the raster, not the PDF the downstream code cannot read.
    assert get_storage().read_bytes(plan["render"]["source_storage_key"])[:4] == b"%PDF"
    with Image.open(io.BytesIO(get_storage().read_bytes(_key(client, headers, plan)))) as image:
        assert image.format == "PNG"


def _key(client: TestClient, headers: dict[str, str], plan: dict) -> str:
    """The plan's storage key, which the API deliberately does not expose."""
    from app.database.session import SessionLocal
    from app.models import FloorPlan

    db = SessionLocal()
    try:
        row = db.get(FloorPlan, plan["id"])
        assert row is not None
        return str(row.storage_key)
    finally:
        db.close()


def test_a_capture_point_can_be_placed_on_a_pdf_sourced_plan(client: TestClient, admin) -> None:
    """The feature that was actually blocked, exercised end to end."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers, "PDF-2")
    plan = _upload(client, headers, floor_id, _pdf("LEVEL 01")).json()

    placed = client.post(
        f"/api/floors/{floor_id}/locations",
        json={"x": 120.0, "y": 340.0, "floor_plan_id": plan["id"], "name": "Lobby"},
        headers=headers,
    )

    assert placed.status_code == 201, placed.text
    assert placed.json()["x"] == 120.0


def test_a_chosen_sheet_is_recorded_on_the_plan(client: TestClient, admin) -> None:
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers, "PDF-3")

    plan = _upload(client, headers, floor_id, _pdf("L01", "L02", "L03"), page="3").json()

    assert plan["render"]["page"] == 3
    assert plan["render"]["page_count"] == 3


def test_a_pdf_sourced_plan_has_no_scale_until_a_human_gives_it_one(
    client: TestClient, admin
) -> None:
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers, "PDF-4")

    plan = _upload(client, headers, floor_id, _pdf("L01")).json()

    assert plan["px_per_meter"] is None
    assert plan["calibration"] is None

    calibrated = client.put(
        f"/api/floorplans/{plan['id']}/calibration",
        json={"x1": 100.0, "y1": 100.0, "x2": 1100.0, "y2": 100.0, "real_distance_m": 25.0},
        headers=headers,
    )

    assert calibrated.status_code == 200, calibrated.text
    body = calibrated.json()
    assert body["px_per_meter"] == pytest.approx(40.0)
    assert body["calibration"]["provenance"] == "manual"
    # The render record survives calibration. It lives in its own column for exactly this
    # reason: `calibration` is replaced wholesale, and losing the page would leave the plan's
    # coordinates unattributable to a sheet.
    assert body["render"]["page"] == 1


def test_a_pdf_renamed_to_png_is_still_refused(client: TestClient, admin) -> None:
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers, "PDF-5")

    response = client.post(
        f"/api/floors/{floor_id}/floorplan",
        files={"file": ("plan.png", _pdf("L01"), "image/png")},
        headers=headers,
    )

    assert response.status_code == 415, response.text


def test_deleting_the_plan_removes_the_source_pdf_too(client: TestClient, admin) -> None:
    """Two objects, one row. Only the row knows where the second one is."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers, "PDF-6")
    plan = _upload(client, headers, floor_id, _pdf("L01")).json()
    source_key = plan["render"]["source_storage_key"]
    assert get_storage().exists(source_key)

    assert client.delete(f"/api/floorplans/{plan['id']}", headers=headers).status_code == 200

    assert not get_storage().exists(source_key)
