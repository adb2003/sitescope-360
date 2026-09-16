"""Chunked, resumable uploads (§7, §24, §32).

The route exists for one failure: a walkthrough video that drops at ninety percent. Everything
here is therefore about what happens when the upload does *not* go smoothly — a part that never
arrives, a part that arrives short, a client that comes back having forgotten what it sent.

The happy path gets one test, because it is the least interesting case and the one that would
pass anyway. What has to be pinned down is that an incomplete upload is refused rather than
assembled: S3 will happily concatenate parts 1, 2 and 4 into a real object, shorter than the
original, decodable for the first two thirds, and wrong in a way no size check or magic-byte
sniff downstream could ever notice.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.storage import get_storage
from app.storage.multipart import (
    DEFAULT_PART_BYTES,
    MAX_PARTS,
    MIN_PART_BYTES,
    ReceivedPart,
    missing_parts,
    plan_parts,
)
from tests.conftest import auth_headers


def _floor(client: TestClient, headers: dict[str, str], code: str) -> str:
    project = client.post(
        "/api/projects", json={"name": "Chunked", "code": code}, headers=headers
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


def _start(client: TestClient, headers: dict[str, str], session_id: str, name: str, size: int):
    return client.post(
        f"/api/captures/{session_id}/multipart",
        json={"filename": name, "size_bytes": size, "content_type": "image/jpeg"},
        headers=headers,
    )


def _ticket(client: TestClient, headers: dict[str, str], session_id: str, name: str, size: int):
    """Start an upload, insisting it worked.

    `.json()` on an unchecked response turns any refusal into a `KeyError` on whichever field
    the test reads next, which says nothing about why the server refused. Asserting the status
    here means a failure names the reason instead.
    """
    response = _start(client, headers, session_id, name, size)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _put_part(client: TestClient, part: dict, body: bytes):
    """PUT one part to its signed URL, as the client would."""
    url = part["upload_url"].replace(settings.public_base_url, "")
    return client.put(url, content=body, headers=part["headers"])


def _body(ticket: dict, name: str, size: int) -> dict:
    return {
        "storage_key": ticket["storage_key"],
        "upload_id": ticket["upload_id"],
        "filename": name,
        "size_bytes": size,
    }


def _complete(client: TestClient, headers: dict[str, str], sid: str, ticket: dict, n: str, s: int):
    return client.post(
        f"/api/captures/{sid}/multipart/complete", json=_body(ticket, n, s), headers=headers
    )


@pytest.fixture
def two_part_file(panorama_factory) -> bytes:
    """A real JPEG, padded past one part boundary.

    The padding is trailing junk, which a JPEG decoder ignores and the magic-byte sniff never
    reaches — so this is a genuinely valid image that happens to need two parts. Synthesising
    8 MiB of noise instead would test the transfer and then fail registration, which is the one
    part of the chain these tests are not about.
    """
    return panorama_factory() + b"\0" * DEFAULT_PART_BYTES


# ---- the arithmetic ------------------------------------------------------------------------
def test_a_file_smaller_than_one_part_is_a_single_part_upload() -> None:
    plan = plan_parts(1024)

    assert plan.part_count == 1
    assert plan.size_of(1) == 1024


def test_the_last_part_carries_the_remainder() -> None:
    plan = plan_parts(DEFAULT_PART_BYTES * 2 + 17)

    assert plan.part_count == 3
    assert plan.size_of(1) == plan.size_of(2) == DEFAULT_PART_BYTES
    assert plan.size_of(3) == 17
    assert sum(plan.size_of(n) for n in range(1, plan.part_count + 1)) == plan.total_bytes


def test_an_exact_multiple_does_not_add_an_empty_final_part() -> None:
    plan = plan_parts(DEFAULT_PART_BYTES * 4)

    assert plan.part_count == 4
    assert plan.size_of(4) == DEFAULT_PART_BYTES


def test_the_part_size_grows_rather_than_the_count_exceeding_the_limit() -> None:
    """S3 allows 10,000 parts. A file large enough to need more must use bigger parts.

    The alternative failure is silent and total: an upload planned with 12,000 parts is rejected
    by S3 at part 10,001, after hours of transfer.
    """
    huge = DEFAULT_PART_BYTES * (MAX_PARTS + 5000)

    plan = plan_parts(huge)

    assert plan.part_count <= MAX_PARTS
    assert plan.part_size > DEFAULT_PART_BYTES
    assert plan.part_size % (1024 * 1024) == 0
    assert plan.part_size * plan.part_count >= huge


def test_a_gap_is_reported_in_order_and_an_upload_with_none_is_empty() -> None:
    received = [ReceivedPart(1, 10), ReceivedPart(4, 10), ReceivedPart(2, 10)]

    assert missing_parts(received, 5) == [3, 5]
    assert missing_parts([ReceivedPart(n, 10) for n in range(1, 4)], 3) == []


# ---- the route -------------------------------------------------------------------------------
@pytest.mark.slow
def test_a_file_uploaded_in_parts_becomes_an_ordinary_capture_file(
    client: TestClient, admin, two_part_file
) -> None:
    """The whole point: indistinguishable downstream from a file sent any other way."""
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers, "MP-1"), "CAP-20260916-01-M001")
    size = len(two_part_file)

    ticket = _ticket(client, headers, session_id, "walk.jpg", size)
    assert ticket["part_count"] == 2
    assert ticket["received"] == []
    assert [part["part_number"] for part in ticket["parts"]] == [1, 2]

    offset = 0
    for part in ticket["parts"]:
        end = offset + part["size_bytes"]
        assert _put_part(client, part, two_part_file[offset:end]).status_code == 201
        offset = end
    assert offset == size

    response = _complete(client, headers, session_id, ticket, "walk.jpg", size)

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["file_size_bytes"] == size
    assert created["kind"] == "panorama"
    # And the assembled object is the original, byte for byte -- the check the part boundaries
    # exist to earn. A transposed or short part would still produce a file of the right length.
    assert get_storage().read_bytes(ticket["storage_key"]) == two_part_file


@pytest.mark.slow
def test_parts_are_assembled_by_number_rather_than_by_arrival(
    client: TestClient, admin, two_part_file
) -> None:
    """Parts arrive out of order routinely — a retry of part 1 lands after part 2.

    Concatenating them in arrival order produces a file of exactly the right length whose halves
    are swapped: no error, no size mismatch, and an image the decoder rejects for reasons that
    look like corruption in transit. Byte equality is the only check that separates the two, and
    it needs the parts to have been sent in the wrong order to mean anything.
    """
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers, "MP-9"), "CAP-20260916-01-M010")
    size = len(two_part_file)
    ticket = _ticket(client, headers, session_id, "walk.jpg", size)
    first, second = ticket["parts"]

    _put_part(client, second, two_part_file[first["size_bytes"] :])
    _put_part(client, first, two_part_file[: first["size_bytes"]])

    assert _complete(client, headers, session_id, ticket, "walk.jpg", size).status_code == 201
    assert get_storage().read_bytes(ticket["storage_key"]) == two_part_file


@pytest.mark.slow
def test_completing_with_a_part_missing_is_refused_and_assembles_nothing(
    client: TestClient, admin, two_part_file
) -> None:
    """The failure that must not be silent.

    Concatenating the parts that did arrive would produce a shorter file that still sniffs as a
    JPEG, still passes the size cap, and still becomes a capture file — a walkthrough with a
    hole in it that nothing downstream can distinguish from a short walk.
    """
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers, "MP-2"), "CAP-20260916-01-M002")
    size = len(two_part_file)
    ticket = _ticket(client, headers, session_id, "walk.jpg", size)

    first = ticket["parts"][0]
    _put_part(client, first, two_part_file[: first["size_bytes"]])

    response = _complete(client, headers, session_id, ticket, "walk.jpg", size)

    assert response.status_code == 409, response.text
    assert "2" in response.json()["message"]
    assert not get_storage().exists(ticket["storage_key"])


@pytest.mark.slow
def test_a_resumed_upload_is_told_what_landed_and_sends_only_the_rest(
    client: TestClient, admin, two_part_file
) -> None:
    """The behaviour the route is named for.

    A client killed mid-transfer has no record of which parts succeeded. The only component that
    does is storage, so the server asks it — and the answer has to be the parts that are really
    there, not the parts anyone believes were sent.
    """
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers, "MP-3"), "CAP-20260916-01-M003")
    size = len(two_part_file)
    ticket = _ticket(client, headers, session_id, "walk.jpg", size)

    first = ticket["parts"][0]
    _put_part(client, first, two_part_file[: first["size_bytes"]])

    resumed = client.post(
        f"/api/captures/{session_id}/multipart/resume",
        json=_body(ticket, "walk.jpg", size),
        headers=headers,
    )

    assert resumed.status_code == 200, resumed.text
    plan = resumed.json()
    assert plan["received"] == [1]
    # Only the missing part comes back with a URL. Re-sending part 1 would work, and would also
    # be the whole cost this route exists to avoid.
    assert [part["part_number"] for part in plan["parts"]] == [2]

    _put_part(client, plan["parts"][0], two_part_file[first["size_bytes"] :])
    assert _complete(client, headers, session_id, ticket, "walk.jpg", size).status_code == 201


@pytest.mark.slow
def test_a_short_middle_part_is_named_here_rather_than_by_the_storage_backend(
    client: TestClient, admin, two_part_file
) -> None:
    """S3 rejects a non-final part under 5 MiB — at completion, with `EntityTooSmall`.

    That is after the entire transfer has been paid for, and the message says nothing about
    which part or why. The same condition is detectable before completion, so it is detected
    before completion.
    """
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers, "MP-4"), "CAP-20260916-01-M004")
    size = len(two_part_file)
    ticket = _ticket(client, headers, session_id, "walk.jpg", size)

    _put_part(client, ticket["parts"][0], two_part_file[:1024])
    _put_part(client, ticket["parts"][1], two_part_file[1024:])

    response = _complete(client, headers, session_id, ticket, "walk.jpg", size)

    assert response.status_code == 422, response.text
    assert str(MIN_PART_BYTES // (1024 * 1024)) in response.json()["message"]


def test_an_upload_larger_than_the_cap_is_refused_before_any_bytes_move(
    client: TestClient, admin
) -> None:
    """The one advantage this route has over a single presigned PUT for validation.

    S3 cannot bound the size of a presigned PUT, so an oversized direct upload is only
    discovered once it has been stored and is then deleted. Here the size is declared up front,
    so it can simply be refused.
    """
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers, "MP-5"), "CAP-20260916-01-M005")

    response = _start(client, headers, session_id, "huge.mp4", settings.max_upload_bytes + 1)

    assert response.status_code == 413, response.text


def test_parts_cannot_be_completed_into_another_sessions_key(client: TestClient, admin) -> None:
    """The key is the server's to choose, and it is checked again at every later step.

    Both the key and the upload id travel through the client, so a client holding a legitimate
    pair for its own session is exactly who would try to complete it somewhere else.
    """
    headers = auth_headers(client, admin.email)
    floor_id = _floor(client, headers, "MP-6")
    mine = _session(client, headers, floor_id, "CAP-20260916-01-M006")
    theirs = _session(client, headers, floor_id, "CAP-20260916-01-M007")
    ticket = _ticket(client, headers, mine, "walk.jpg", 4096)

    response = _complete(client, headers, theirs, ticket, "walk.jpg", 4096)

    assert response.status_code == 422, response.text
    assert "does not belong" in response.json()["message"]


def test_an_unsupported_extension_never_starts_an_upload(client: TestClient, admin) -> None:
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers, "MP-7"), "CAP-20260916-01-M008")

    response = _start(client, headers, session_id, "notes.exe", 4096)

    assert response.status_code == 415, response.text


@pytest.mark.slow
def test_aborting_discards_the_parts(client: TestClient, admin, two_part_file) -> None:
    """Parts are billable and invisible: not objects, not listed, and never looked for again."""
    headers = auth_headers(client, admin.email)
    session_id = _session(client, headers, _floor(client, headers, "MP-8"), "CAP-20260916-01-M009")
    size = len(two_part_file)
    ticket = _ticket(client, headers, session_id, "walk.jpg", size)
    first = ticket["parts"][0]
    _put_part(client, first, two_part_file[: first["size_bytes"]])

    aborted = client.post(
        f"/api/captures/{session_id}/multipart/abort",
        json=_body(ticket, "walk.jpg", size),
        headers=headers,
    )

    assert aborted.status_code == 200, aborted.text
    resumed = client.post(
        f"/api/captures/{session_id}/multipart/resume",
        json=_body(ticket, "walk.jpg", size),
        headers=headers,
    )
    assert resumed.status_code == 404, resumed.text
