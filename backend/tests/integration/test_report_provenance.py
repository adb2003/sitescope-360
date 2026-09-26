"""What a report says about where its positions came from (§29, §46).

A report is the one artefact that leaves the building. It reaches a client, an owner, a
subcontractor — people who will never open the interface that explains what "estimated" means
here, and who have only the document in front of them. So the provenance block is not a
disclaimer to be satisfied by mentioning the possibility of estimation: it has to describe
*these* positions.

The note it replaces was written when a position could arrive two ways — a person placed the
point, or a transform they anchored did — and was accurate. Cross-session recognition added a
third that carries a caveat neither of the others does: it matches an earlier capture's point to
within a viewpoint tolerance, and chains of that compound. A static sentence cannot say how much
of that happened in a particular report, so these tests are about the note being derived rather
than declared.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth_headers
from tests.integration.test_cross_session import (
    REVISIT,
    WALK,
    _anchored_first_capture,
    _capture,
)


def _project_floor(client: TestClient, headers: dict[str, str], code: str) -> tuple[str, str]:
    project = client.post(
        "/api/projects", json={"name": f"Report {code}", "code": code}, headers=headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "Block A"}, headers=headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "Level 01", "level": 1, "area_sqft": 5000.0},
        headers=headers,
    ).json()
    return str(project["id"]), str(floor["id"])


def _report(client: TestClient, headers: dict[str, str], project_id: str, kind: str) -> dict:
    created = client.post(
        f"/api/projects/{project_id}/reports",
        json={"report_type": kind, "report_format": "json"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = client.get(f"/api/reports/{created.json()['id']}/download", headers=headers)
    assert body.status_code == 200, body.text
    return dict(body.json())


@pytest.fixture
def mixed_floor(client: TestClient, admin, panorama_factory):
    """One floor, one capture anchored by hand and one that recognised itself against it."""
    headers = auth_headers(client, admin.email)
    project_id, floor_id = _project_floor(client, headers, "RP-1")
    _anchored_first_capture(client, headers, floor_id, panorama_factory, "R1")
    _capture(client, headers, floor_id, panorama_factory, "CAP-20260908-090000-R2", REVISIT)
    return headers, project_id, floor_id


def test_the_positions_note_counts_what_happened_in_this_report(mixed_floor, client) -> None:
    """Derived, not declared. The reader is told about these captures, not about the software."""
    headers, project_id, _floor_id = mixed_floor

    payload = _report(client, headers, project_id, "project_summary")

    positions = payload["provenance"]["positions"]
    assert "1 anchored by hand" in positions, positions
    assert "recognised against earlier captures" in positions, positions
    # The caveat that only applies because recognition was involved.
    assert "viewpoint tolerance" in positions, positions


def test_a_report_with_nothing_placed_says_exactly_that(
    client: TestClient, admin, panorama_factory
) -> None:
    """A capture nobody has placed is not described by "estimated or manual"."""
    headers = auth_headers(client, admin.email)
    project_id, floor_id = _project_floor(client, headers, "RP-2")
    _capture(client, headers, floor_id, panorama_factory, "CAP-20260901-090000-R3", WALK)

    payload = _report(client, headers, project_id, "project_summary")

    assert "not placed" in payload["provenance"]["positions"]


def test_each_capture_row_names_its_own_route_onto_the_plan(mixed_floor, client) -> None:
    """The summary at the foot of a report is not enough when reading one line of a table."""
    headers, project_id, _floor_id = mixed_floor

    payload = _report(client, headers, project_id, "capture_history")

    placements = {row["placement"] for row in payload["rows"]}
    assert "anchored by hand" in placements, placements
    assert any(value.startswith("recognised from earlier captures") for value in placements), (
        placements
    )


def test_the_note_survives_the_csv_export(mixed_floor, client) -> None:
    """CSV is how these numbers actually reach a spreadsheet, and the caveat has to come too."""
    headers, project_id, _floor_id = mixed_floor
    created = client.post(
        f"/api/projects/{project_id}/reports",
        json={"report_type": "capture_history", "report_format": "csv"},
        headers=headers,
    ).json()

    csv_body = client.get(f"/api/reports/{created['id']}/download", headers=headers).text

    assert "recognised against earlier captures" in csv_body
    assert "anchored by hand" in csv_body
