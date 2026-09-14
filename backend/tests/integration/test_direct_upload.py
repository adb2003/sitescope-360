"""Uploading straight to storage, with the API only at the ends (§7, §24).

`store_upload` gets its safety for free: the extension check, the magic-byte sniff and the size
cap all run while the bytes stream past, so a rejected upload never becomes an object. A direct
upload inverts that -- the client writes first and the server inspects afterwards -- and the
guarantee has to be rebuilt by hand.

So most of what follows is about the rebuild, not the happy path. Each test names something the
proxied route enforces for free and checks that this route reaches the same end state: nothing
recorded, and nothing left in storage.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.storage import get_storage
from tests.conftest import auth_headers


def _floor(client: TestClient, headers: dict[str, str], code: str = "DIR-1") -> str:
    project = client.post(
        "/api/projects", json={"name": "Direct", "code": code}, headers=headers
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


def _session(client: TestClient, headers: dict[str, str], floor_id: str, client_id: str) -> str:
    return str(
        client.post(
            "/api/captures",
            json={"floor_id": floor_id, "client_session_id": client_id},
            headers=headers,
        ).json()["id"]
    )


def _ticket(client: TestClient, headers: dict[str, str], session_id: str, filename: str) -> dict:
    response = client.post(
        f"/api/captures/{session_id}/upload-url",
        json={"filename": filename, "content_type": "image/jpeg"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _put(client: TestClient, ticket: dict, body: bytes):
    """PUT to the signed URL, as the client would. TestClient needs the path, not the origin."""
    url = ticket["upload_url"].replace(settings.public_base_url, "")
    return client.put(url, content=body, headers=ticket["headers"])


def _register(client: TestClient, headers: dict[str, str], session_id: str, key: str, name: str):
    return client.post(
        f"/api/captures/{session_id}/files",
        json={"storage_key": key, "filename": name},
        headers=headers,
    )


def test_the_whole_direct_path_produces_a_normal_capture_file(
    client: TestClient, admin, panorama_factory
) -> None:
    """A file uploaded this way must be indistinguishable downstream from a proxied one."""
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers), "CAP-20260914-090000-D001")
    body = panorama_factory((0.0, 0.0, 0.0))

    ticket = _ticket(client, headers, session_id, "WALK_001.jpg")
    assert _put(client, ticket, body).status_code == 201

    registered = _register(client, headers, session_id, ticket["storage_key"], "WALK_001.jpg")
    assert registered.status_code == 201, registered.text
    record = registered.json()
    assert record["file_size_bytes"] == len(body)
    assert record["content_type"] == "image/jpeg"

    listed = client.get(f"/api/captures/{session_id}/files", headers=headers).json()
    assert [item["id"] for item in listed] == [record["id"]]
    # And the pipeline will accept it: the session is ready to finish and process.
    assert client.post(f"/api/captures/{session_id}/finish", headers=headers).status_code == 200


def test_the_server_chooses_the_key_not_the_client(client: TestClient, admin) -> None:
    """A client-named key is a client-chosen write target; no signature makes that safe."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    session_id = _session(client, headers, floor_id, "CAP-20260914-090100-D002")

    key = _ticket(client, headers, session_id, "WALK.jpg")["storage_key"]

    assert key.startswith("projects/")
    assert f"/sessions/{session_id}/source/" in key
    # The stored name is a fresh id, never the client's filename.
    assert PurePosixPath(key).stem != "WALK"


def test_a_key_from_another_session_cannot_be_registered(client: TestClient, admin) -> None:
    """Otherwise one session could claim another's bytes as its own capture."""
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers)
    mine = _session(client, headers, floor_id, "CAP-20260914-090200-D003")
    theirs = _session(client, headers, floor_id, "CAP-20260914-090300-D004")

    stolen = _ticket(client, headers, theirs, "THEIRS.jpg")["storage_key"]
    refused = _register(client, headers, mine, stolen, "THEIRS.jpg")

    assert refused.status_code == 422
    assert "session" in refused.text.lower()


def test_an_unsupported_extension_never_gets_a_url_at_all(client: TestClient, admin) -> None:
    """The cheapest check still runs first: no URL is issued, so nothing can be written."""
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers), "CAP-20260914-090400-D005")

    refused = client.post(
        f"/api/captures/{session_id}/upload-url",
        json={"filename": "payload.exe"},
        headers=headers,
    )

    assert refused.status_code == 415


def test_a_disguised_file_is_rejected_and_deleted(client: TestClient, admin) -> None:
    """The sniff `store_upload` does before storing, done after -- same end state.

    Deleting matters as much as refusing. An object left behind is one the uploader still pays
    for, and one a later bug could pick up as a real capture.
    """
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers), "CAP-20260914-090500-D006")

    ticket = _ticket(client, headers, session_id, "NOT_REALLY.jpg")
    landed = _put(client, ticket, b"MZ\x90\x00this is an executable, not a panorama")
    assert landed.status_code == 201
    assert get_storage().exists(ticket["storage_key"]), "precondition: the bytes landed"

    refused = _register(client, headers, session_id, ticket["storage_key"], "NOT_REALLY.jpg")

    assert refused.status_code == 415
    assert not get_storage().exists(ticket["storage_key"]), "the rejected object was left behind"
    assert client.get(f"/api/captures/{session_id}/files", headers=headers).json() == []


def test_an_empty_object_is_rejected_and_deleted(client: TestClient, admin) -> None:
    """A zero-byte PUT is what an interrupted upload leaves; it must not become a capture file."""
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers), "CAP-20260914-090600-D007")

    ticket = _ticket(client, headers, session_id, "EMPTY.jpg")
    assert _put(client, ticket, b"").status_code == 201

    refused = _register(client, headers, session_id, ticket["storage_key"], "EMPTY.jpg")

    assert refused.status_code == 422
    assert not get_storage().exists(ticket["storage_key"])


def test_registering_before_uploading_is_a_404_not_an_empty_file(client: TestClient, admin) -> None:
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers), "CAP-20260914-090700-D008")
    ticket = _ticket(client, headers, session_id, "MISSING.jpg")

    missing = _register(client, headers, session_id, ticket["storage_key"], "MISSING.jpg")

    assert missing.status_code == 404


def test_registering_twice_yields_one_file(client: TestClient, admin, panorama_factory) -> None:
    """A client that retries after a lost response must not create a second row."""
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers), "CAP-20260914-090800-D009")

    ticket = _ticket(client, headers, session_id, "RETRY.jpg")
    _put(client, ticket, panorama_factory((0.0, 0.0, 0.0)))

    first = _register(client, headers, session_id, ticket["storage_key"], "RETRY.jpg")
    second = _register(client, headers, session_id, ticket["storage_key"], "RETRY.jpg")

    assert first.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert len(client.get(f"/api/captures/{session_id}/files", headers=headers).json()) == 1


def test_a_filename_that_disagrees_with_the_key_is_refused(
    client: TestClient, admin, panorama_factory
) -> None:
    """The key was minted from that extension, and stages stage temp files by the key's suffix."""
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers), "CAP-20260914-090900-D010")

    ticket = _ticket(client, headers, session_id, "WALK.jpg")
    _put(client, ticket, panorama_factory((0.0, 0.0, 0.0)))

    refused = _register(client, headers, session_id, ticket["storage_key"], "WALK.mp4")

    assert refused.status_code == 422


def test_a_read_link_cannot_be_replayed_as_an_upload(
    client: TestClient, admin, panorama_factory
) -> None:
    """The signature covers the verb, so asset URLs in flight are not also write tokens.

    Without that domain separation this is the whole vulnerability: every panorama URL a browser
    has ever been handed would authorise overwriting the object it points at.
    """
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers), "CAP-20260914-091000-D011")
    body = panorama_factory((0.0, 0.0, 0.0))

    ticket = _ticket(client, headers, session_id, "WALK.jpg")
    _put(client, ticket, body)
    created = _register(client, headers, session_id, ticket["storage_key"], "WALK.jpg").json()

    # A signed *read* URL for that stored object, exactly as the viewer receives it.
    detail = client.get(f"/api/captures/{session_id}/files", headers=headers).json()
    read_url = next(item for item in detail if item["id"] == created["id"])["asset"]["url"]

    replayed = client.put(read_url.replace(settings.public_base_url, ""), content=b"overwritten")

    assert replayed.status_code == 403
    assert get_storage().read_bytes(ticket["storage_key"]) == body


def test_a_tampered_upload_link_is_refused(client: TestClient, admin) -> None:
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers), "CAP-20260914-091100-D012")
    ticket = _ticket(client, headers, session_id, "WALK.jpg")
    url = ticket["upload_url"].replace(settings.public_base_url, "")

    tampered = client.put(url.replace("signature=", "signature=x"), content=b"bytes")

    assert tampered.status_code == 403
    assert not get_storage().exists(ticket["storage_key"])


def test_an_upload_past_the_size_cap_leaves_nothing_behind(
    client: TestClient, admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enforced while streaming, not from Content-Length, which the client controls."""
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers), "CAP-20260914-091200-D013")
    ticket = _ticket(client, headers, session_id, "HUGE.jpg")
    monkeypatch.setattr(settings, "max_upload_bytes", 1024)

    refused = _put(client, ticket, b"\xff" * 4096)

    assert refused.status_code == 413
    assert not get_storage().exists(ticket["storage_key"])
