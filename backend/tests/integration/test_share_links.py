"""Read-only links to a single view (§27).

A share link is a bearer credential: whoever holds it has the access, with no account and no
membership. That is the point — a client signing off a slab should not need a login — and it is
why almost every test here is about a boundary rather than about the happy path.

Three properties carry the feature, and each has a test whose failure would be a real incident:

* the token is **never stored in the clear**, so a copy of the table is not a set of working
  links;
* the scope is **one frame**, so a link is not a map of the project it came from;
* every refusal is **the same refusal**, so a dead token tells its holder nothing about whether
  it was ever alive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.conftest import PASSWORD, auth_headers, join_project
from tests.integration.test_definition_of_done import _await_ready


@pytest.fixture
def shared_frame(client: TestClient, admin_headers: dict[str, str], panorama_factory):
    """A processed capture on its own project, and the id of one of its frames."""
    project = client.post(
        "/api/projects", json={"name": "Share", "code": "SHARE-1"}, headers=admin_headers
    ).json()
    building = client.post(
        f"/api/projects/{project['id']}/buildings", json={"name": "Block A"}, headers=admin_headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors",
        json={"name": "Level 01", "level": 1},
        headers=admin_headers,
    ).json()
    session_id = client.post(
        "/api/captures", json={"floor_id": floor["id"]}, headers=admin_headers
    ).json()["id"]
    client.post(
        f"/api/captures/{session_id}/upload",
        files={"files": ("a.jpg", panorama_factory(), "image/jpeg")},
        headers=admin_headers,
    )
    client.post(f"/api/captures/{session_id}/finish", headers=admin_headers)
    client.post(f"/api/captures/{session_id}/process", json={}, headers=admin_headers)
    _await_ready(client, admin_headers, session_id)
    frames = client.get(f"/api/captures/{session_id}/frames", headers=admin_headers).json()["items"]
    return project, frames[0]["id"]


def _share(client: TestClient, headers: dict[str, str], frame_id: str, **body) -> dict:
    response = client.post(f"/api/frames/{frame_id}/share", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# ---- the capability ----------------------------------------------------------
def test_a_link_opens_the_view_with_no_account_at_all(client, admin_headers, shared_frame) -> None:
    """The feature. No Authorization header anywhere in this request."""
    _project, frame_id = shared_frame
    link = _share(client, admin_headers, frame_id)

    opened = client.get(f"/api/shared/{link['token']}")

    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["floor_name"] == "Level 01"
    assert body["full"]["url"], "the image has to be reachable or the link is pointless"
    # The caveats travel in the payload, not only in whatever page renders it.
    assert "read-only" in body["notice"].lower()
    assert "estimated" in body["notice"].lower()


def test_the_shared_view_is_one_frame_and_not_a_map_of_the_project(
    client, admin_headers, shared_frame
) -> None:
    """A link to one view must not become a way to browse the job it came from.

    Asserted as an absence, which is the only way to assert it: the risk is not that the payload
    says something wrong but that it quietly grows a field — siblings, annotations, a capture
    path — the next time somebody extends the viewer manifest and reuses this schema.
    """
    _project, frame_id = shared_frame
    link = _share(client, admin_headers, frame_id)

    body = client.get(f"/api/shared/{link['token']}").json()

    for leak in ("frames", "siblings", "annotations", "path", "next_frame_id", "session_id"):
        assert leak not in body, f"{leak} would turn one view into navigation"
    # Not even the frame's own id: there is nothing a visitor could do with it, and its absence
    # is what keeps this payload from being mistaken for an authenticated one.
    assert "id" not in body
    assert "project_id" not in body


def test_the_token_is_returned_once_and_never_again(client, admin_headers, shared_frame) -> None:
    """Only the hash is stored, so "show me that link again" cannot be answered — by design."""
    _project, frame_id = shared_frame
    link = _share(client, admin_headers, frame_id, label="for the QS")

    listed = client.get(f"/api/frames/{frame_id}/shares", headers=admin_headers).json()

    assert len(listed) == 1
    assert listed[0]["label"] == "for the QS"
    assert "token" not in listed[0]
    assert (
        link["token"]
        not in client.get(f"/api/frames/{frame_id}/shares", headers=admin_headers).text
    )


def test_the_token_is_not_stored_in_the_clear(client, admin_headers, shared_frame, db) -> None:
    """A dump of `share_links` must not be a set of working links."""
    from app.models import ShareLink
    from app.services.sharing import hash_token

    _project, frame_id = shared_frame
    link = _share(client, admin_headers, frame_id)

    rows = list(db.query(ShareLink).all()) if hasattr(db, "query") else None
    if rows is None:  # pragma: no cover - SQLAlchemy 2.0 style
        from sqlalchemy import select

        rows = list(db.scalars(select(ShareLink)))
    stored = {row.token_hash for row in rows}
    assert link["token"] not in stored
    assert hash_token(link["token"]) in stored


def test_views_are_counted(client, admin_headers, shared_frame) -> None:
    """How someone decides about a link they are unsure of: one never opened is easy to revoke."""
    _project, frame_id = shared_frame
    link = _share(client, admin_headers, frame_id)

    client.get(f"/api/shared/{link['token']}")
    client.get(f"/api/shared/{link['token']}")

    listed = client.get(f"/api/frames/{frame_id}/shares", headers=admin_headers).json()
    assert listed[0]["view_count"] == 2
    assert listed[0]["last_viewed_at"] is not None


# ---- the refusals ------------------------------------------------------------
def test_a_wrong_token_an_expired_one_and_a_revoked_one_are_indistinguishable(
    client, admin_headers, shared_frame, db
) -> None:
    """The one test that would be an incident if it failed.

    Telling a visitor that a link *has expired* confirms it was once real — the single most
    useful fact to anyone probing, and no help at all to the person it was sent to, who has to
    ask the sender for a new one either way. So all three answers must be byte-identical.
    """
    from sqlalchemy import select

    from app.models import ShareLink
    from app.services.sharing import hash_token

    _project, frame_id = shared_frame
    expired = _share(client, admin_headers, frame_id)
    revoked = _share(client, admin_headers, frame_id)

    row = db.scalar(select(ShareLink).where(ShareLink.token_hash == hash_token(expired["token"])))
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    client.delete(f"/api/shares/{revoked['id']}", headers=admin_headers)

    answers = [
        client.get("/api/shared/there-is-no-such-token-at-all-here"),
        client.get(f"/api/shared/{expired['token']}"),
        client.get(f"/api/shared/{revoked['token']}"),
    ]

    assert [response.status_code for response in answers] == [404, 404, 404]
    bodies = {response.json()["message"] for response in answers}
    assert len(bodies) == 1, bodies


def test_revoking_stops_a_link_immediately(client, admin_headers, shared_frame) -> None:
    """Independent of expiry, because the reason to revoke is usually that it went astray."""
    _project, frame_id = shared_frame
    link = _share(client, admin_headers, frame_id, expires_in_days=90)
    assert client.get(f"/api/shared/{link['token']}").status_code == 200

    revoked = client.delete(f"/api/shares/{link['id']}", headers=admin_headers)

    assert revoked.status_code == 200, revoked.text
    assert client.get(f"/api/shared/{link['token']}").status_code == 404


def test_revoking_twice_is_not_an_error(client, admin_headers, shared_frame) -> None:
    """The second caller wants the same outcome; an error would only make them re-check."""
    _project, frame_id = shared_frame
    link = _share(client, admin_headers, frame_id)

    client.delete(f"/api/shares/{link['id']}", headers=admin_headers)
    again = client.delete(f"/api/shares/{link['id']}", headers=admin_headers)

    assert again.status_code == 200, again.text


def test_a_link_cannot_outlive_the_cap(client, admin_headers, shared_frame) -> None:
    """A link lasting a year is a link with no expiry, only more slowly."""
    _project, frame_id = shared_frame

    too_long = client.post(
        f"/api/frames/{frame_id}/share", json={"expires_in_days": 400}, headers=admin_headers
    )
    too_short = client.post(
        f"/api/frames/{frame_id}/share", json={"expires_in_days": 0}, headers=admin_headers
    )

    assert too_long.status_code == 422, too_long.text
    assert too_short.status_code == 422, too_short.text


def test_a_deleted_frame_takes_its_links_with_it(
    client, admin_headers, shared_frame, panorama_factory
) -> None:
    """Nothing to show is the same as nothing to find."""
    _project, frame_id = shared_frame
    link = _share(client, admin_headers, frame_id)
    session_id = client.get(f"/api/frames/{frame_id}", headers=admin_headers).json()["session_id"]

    client.delete(f"/api/captures/{session_id}", headers=admin_headers)

    assert client.get(f"/api/shared/{link['token']}").status_code == 404


# ---- who may share -----------------------------------------------------------
def test_you_cannot_share_a_frame_from_a_project_you_are_not_on(
    client, admin_headers, shared_frame, viewer
) -> None:
    """Sharing is a way to hand out access, so it needs at least as much as reading does (§27)."""
    _project, frame_id = shared_frame
    headers = auth_headers(client, viewer.email)

    refused = client.post(f"/api/frames/{frame_id}/share", json={}, headers=headers)

    assert refused.status_code == 404, refused.text


def test_you_cannot_revoke_a_link_in_a_project_you_are_not_on(
    client, admin_headers, shared_frame, viewer
) -> None:
    _project, frame_id = shared_frame
    link = _share(client, admin_headers, frame_id)
    headers = auth_headers(client, viewer.email)

    refused = client.delete(f"/api/shares/{link['id']}", headers=headers)

    assert refused.status_code == 404, refused.text
    # And it still works, which is the point of checking.
    assert client.get(f"/api/shared/{link['token']}").status_code == 200


def test_any_member_can_revoke_not_only_the_author(
    client, admin_headers, shared_frame, viewer
) -> None:
    """A link only its author can revoke is a link that outlives them leaving the project."""
    project, frame_id = shared_frame
    link = _share(client, admin_headers, frame_id)
    join_project(client, admin_headers, project["id"], viewer.id)
    headers = auth_headers(client, viewer.email)

    revoked = client.delete(f"/api/shares/{link['id']}", headers=headers)

    assert revoked.status_code == 200, revoked.text
    assert client.get(f"/api/shared/{link['token']}").status_code == 404


def test_a_projects_links_can_be_audited_in_one_place(client, admin_headers, shared_frame) -> None:
    """The question someone asks after a link leaks: what is out there for this job."""
    project, frame_id = shared_frame
    _share(client, admin_headers, frame_id, label="one")
    second = _share(client, admin_headers, frame_id, label="two")
    client.delete(f"/api/shares/{second['id']}", headers=admin_headers)

    listed = client.get(f"/api/projects/{project['id']}/shares", headers=admin_headers).json()

    assert {row["label"] for row in listed} == {"one", "two"}
    # Dead links are listed too: hiding them answers "was this ever shared" wrongly.
    assert {row["is_live"] for row in listed} == {True, False}


def test_a_token_is_never_written_to_the_logs(client, admin_headers, shared_frame) -> None:
    """A token in a URL reaches everywhere the URL reaches, including a log shipper.

    Both error handlers log `request.url.path`, so a 500 while opening a link would write a
    *live* credential into logs kept longer and read more widely than the database. The path is
    redacted before it gets there.
    """
    from app.main import safe_path

    class _Request:
        def __init__(self, path: str) -> None:
            self.url = type("U", (), {"path": path})()

    _project, frame_id = shared_frame
    link = _share(client, admin_headers, frame_id)

    logged = safe_path(_Request(f"/api/shared/{link['token']}"))

    assert link["token"] not in logged
    assert logged == "/api/shared/<redacted>"
    # An ordinary path is left alone: redaction that swallowed everything would make every other
    # error harder to diagnose for no gain.
    assert safe_path(_Request("/api/captures/CAP-1/frames")) == "/api/captures/CAP-1/frames"


def test_opening_links_never_spends_the_sign_in_allowance(client, admin_headers) -> None:
    """Two endpoints in one bucket share a fate, and this pair shared the wrong one.

    Opening a shared view used to count against `ratelimit:auth:addr:` — the sign-in bucket. A
    link passed around a site team, who are all behind one office address, would quietly consume
    everybody's login allowance; and anyone who hit the limit was told there had been "too many
    sign-in attempts", which is nonsense to a visitor with no account.

    The loop runs past the auth limit deliberately. Under the old behaviour the login below
    would have been refused.
    """
    from app.core.config import settings

    for _ in range(settings.auth_address_max_attempts + 5):
        # Invalid tokens: the limiter is spent before the token is looked at, which is the whole
        # point — a refusal must not depend on whether the link was real.
        assert client.get("/api/shared/not-a-real-token-but-long-enough").status_code == 404

    signed_in = client.post(
        "/api/auth/login",
        json={"email": "admin@test.example.com", "password": PASSWORD},
    )

    assert signed_in.status_code == 200, signed_in.text


def test_the_share_limit_does_not_talk_about_signing_in(client, monkeypatch) -> None:
    """The message a visitor gets has to make sense to someone who has no account."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "share_address_max_views", 2)
    for _ in range(2):
        client.get("/api/shared/not-a-real-token-but-long-enough")

    refused = client.get("/api/shared/not-a-real-token-but-long-enough")

    assert refused.status_code == 429, refused.text
    body = refused.json()
    assert "sign-in" not in body["message"].lower()
    assert "link" in body["message"].lower()
    assert refused.headers["Retry-After"]
