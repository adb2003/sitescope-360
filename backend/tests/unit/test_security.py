"""Security-relevant behaviour: storage keys, signed URLs, passwords, tokens (§44)."""

from __future__ import annotations

import itertools
import time
from pathlib import Path

import pytest

from app.auth.rbac import Capability, capabilities_for, has_capability, role_at_least
from app.auth.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.core.enums import Role
from app.core.errors import AuthenticationError, ValidationError
from app.storage.base import build_key, validate_key
from app.storage.local import LocalStorage


# ---- storage key validation --------------------------------------------------
@pytest.mark.parametrize(
    "segments",
    [
        ("projects", "..", "etc"),
        ("projects", "../../etc/passwd"),
        ("projects", "a/b"),
        ("projects", ""),
        ("projects", "."),
        ("projects", "with space"),
        ("projects", "semi;colon"),
        ("projects", "back\\slash"),
        ("projects", "nul\x00byte"),
    ],
)
def test_build_key_rejects_unsafe_segments(segments: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        build_key(*segments)


def test_build_key_accepts_expected_shapes() -> None:
    key = build_key("projects", "abc123", "floors", "def456", "source", "file.jpg")
    assert key == "projects/abc123/floors/def456/source/file.jpg"


@pytest.mark.parametrize(
    "key",
    [
        "/absolute/path",
        "..\\windows",
        "a/../../b",
        "a/./b",
        "",
        "C:\\Windows\\System32",
    ],
)
def test_validate_key_rejects_traversal(key: str) -> None:
    with pytest.raises(ValidationError):
        validate_key(key)


def test_local_storage_cannot_escape_its_root(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path / "storage", signing_secret="s" * 32)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")

    for attempt in ("../secret.txt", "../../secret.txt", "/etc/passwd"):
        with pytest.raises(Exception):  # noqa: B017 - any refusal is acceptable
            storage.read_bytes(attempt)


def test_local_storage_roundtrip(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path / "storage", signing_secret="s" * 32)
    key = build_key("projects", "p1", "source", "a.jpg")

    stored = storage.put_bytes(key, b"hello world", "image/jpeg")
    assert stored.size_bytes == 11
    assert storage.exists(key)
    assert storage.read_bytes(key) == b"hello world"
    assert storage.size(key) == 11
    assert storage.local_path(key) is not None

    storage.delete(key)
    assert not storage.exists(key)


def test_local_storage_writes_atomically(tmp_path: Path) -> None:
    """No partial ``.part`` file should survive a completed write."""
    storage = LocalStorage(root=tmp_path / "storage", signing_secret="s" * 32)
    key = build_key("projects", "p1", "source", "a.jpg")
    storage.put_bytes(key, b"x" * 4096, "image/jpeg")

    leftovers = list((tmp_path / "storage").rglob("*.part"))
    assert leftovers == []


def test_delete_prefix_removes_a_whole_subtree(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path / "storage", signing_secret="s" * 32)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        storage.put_bytes(build_key("projects", "p1", "source", name), b"x", "image/jpeg")
    storage.put_bytes(build_key("projects", "p2", "source", "keep.jpg"), b"x", "image/jpeg")

    removed = storage.delete_prefix(build_key("projects", "p1"))
    assert removed == 3
    assert storage.exists(build_key("projects", "p2", "source", "keep.jpg"))


# ---- signed URLs -------------------------------------------------------------
def test_signature_is_required_and_scoped_to_the_key(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path / "storage", signing_secret="s" * 32)
    key_a = build_key("projects", "p1", "source", "a.jpg")
    key_b = build_key("projects", "p1", "source", "b.jpg")

    expires, signature = storage.sign(key_a, 60)
    assert storage.verify(key_a, expires, signature)
    # A signature for one object must not unlock another.
    assert not storage.verify(key_b, expires, signature)
    assert not storage.verify(key_a, expires, "tampered")
    assert not storage.verify(key_a, expires + 1, signature)


def test_expired_signature_is_rejected(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path / "storage", signing_secret="s" * 32)
    key = build_key("projects", "p1", "source", "a.jpg")
    expired = int(time.time()) - 10
    signature = storage._signature(key, expired)
    assert not storage.verify(key, expired, signature)


def test_a_different_secret_cannot_forge_a_signature(tmp_path: Path) -> None:
    real = LocalStorage(root=tmp_path / "storage", signing_secret="real-secret" * 4)
    attacker = LocalStorage(root=tmp_path / "storage", signing_secret="guessed" * 6)
    key = build_key("projects", "p1", "source", "a.jpg")

    expires, forged = attacker.sign(key, 60)
    assert not real.verify(key, expires, forged)


def test_signed_url_never_contains_a_filesystem_path(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path / "storage", signing_secret="s" * 32)
    key = build_key("projects", "p1", "source", "a.jpg")
    url = storage.signed_url(key)

    assert str(tmp_path) not in url
    assert "signature=" in url and "expires=" in url


# ---- passwords ---------------------------------------------------------------
def test_password_hashing_roundtrip() -> None:
    digest = hash_password("correct horse battery staple")
    assert digest != "correct horse battery staple"
    assert verify_password("correct horse battery staple", digest)
    assert not verify_password("wrong password", digest)


def test_long_passphrases_stay_fully_significant() -> None:
    """bcrypt truncates at 72 bytes; pre-hashing means a 100-char passphrase is not equivalent
    to its first 72 characters."""
    base = "a" * 72
    digest = hash_password(base + "DIFFERENT-TAIL")
    assert not verify_password(base + "ANOTHER-TAIL!!", digest)
    assert verify_password(base + "DIFFERENT-TAIL", digest)


def test_verify_password_tolerates_a_malformed_hash() -> None:
    assert not verify_password("anything", "not-a-bcrypt-hash")
    assert not verify_password("anything", "")


def test_hashes_are_salted() -> None:
    assert hash_password("same") != hash_password("same")


# ---- tokens ------------------------------------------------------------------
def test_access_token_roundtrip() -> None:
    token, expires = create_access_token(
        user_id="u1", email="a@example.com", role=Role.PROJECT_MANAGER
    )
    claims = decode_access_token(token)

    assert claims.subject == "u1"
    assert claims.role is Role.PROJECT_MANAGER
    assert claims.email == "a@example.com"
    assert claims.expires_at == expires.replace(microsecond=0)


def test_tampered_token_is_rejected() -> None:
    token, _ = create_access_token(user_id="u1", email="a@example.com", role=Role.VIEWER)
    head, payload, signature = token.split(".")

    with pytest.raises(AuthenticationError):
        decode_access_token(f"{head}.{payload}.{signature[:-4]}xxxx")
    with pytest.raises(AuthenticationError):
        decode_access_token("garbage")
    with pytest.raises(AuthenticationError):
        decode_access_token("")


def test_refresh_token_only_its_hash_is_storable() -> None:
    plaintext, digest, expires = new_refresh_token()
    assert plaintext not in digest
    assert len(digest) == 64
    assert hash_refresh_token(plaintext) == digest
    assert expires.tzinfo is not None


# ---- RBAC --------------------------------------------------------------------
def test_role_ordering() -> None:
    assert role_at_least(Role.ADMIN, Role.VIEWER)
    assert role_at_least(Role.PROJECT_MANAGER, Role.SITE_ENGINEER)
    assert not role_at_least(Role.SITE_ENGINEER, Role.PROJECT_MANAGER)
    assert role_at_least(Role.VIEWER, Role.VIEWER)


def test_capability_matrix_matches_the_specification() -> None:
    """The §27 table, asserted directly."""
    # Viewer: view only.
    assert capabilities_for(Role.VIEWER) == {
        Capability.PROJECT_READ,
        Capability.CAPTURE_READ,
    }

    # Site Engineer: capture / upload / view — and annotate, but not manage projects.
    engineer = capabilities_for(Role.SITE_ENGINEER)
    assert Capability.UPLOAD in engineer
    assert Capability.CAPTURE_CREATE in engineer
    assert Capability.ANNOTATE in engineer
    assert Capability.PROJECT_WRITE not in engineer
    assert Capability.ANNOTATION_RESOLVE not in engineer

    # Project Manager: create/manage projects and captures, but not system settings or users.
    manager = capabilities_for(Role.PROJECT_MANAGER)
    assert Capability.PROJECT_WRITE in manager
    assert Capability.REPORT_GENERATE in manager
    assert Capability.SETTINGS_PROJECT in manager
    assert Capability.SETTINGS_SYSTEM not in manager
    assert Capability.USER_MANAGE not in manager

    # Admin: everything.
    assert capabilities_for(Role.ADMIN) == frozenset(Capability)


def test_capabilities_grow_monotonically_with_role() -> None:
    """A more privileged role must never lose a capability a lesser role has."""
    order = [Role.VIEWER, Role.SITE_ENGINEER, Role.PROJECT_MANAGER, Role.ADMIN]
    for lower, higher in itertools.pairwise(order):
        assert capabilities_for(lower) <= capabilities_for(higher), (lower, higher)


def test_has_capability() -> None:
    assert has_capability(Role.ADMIN, Capability.USER_MANAGE)
    assert not has_capability(Role.VIEWER, Capability.UPLOAD)
