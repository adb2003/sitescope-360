"""Shared test fixtures.

Each test module gets its own temporary database *and* its own temporary storage root, so tests
cannot see each other's objects and a failing test leaves nothing behind.

Environment variables are set before any application module is imported. ``app.core.config``
builds its settings at import time and caches them, so configuring afterwards would silently have
no effect — which is the kind of failure that makes a suite pass while testing the wrong thing.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="sitescope-tests-"))

os.environ.update(
    ENVIRONMENT="test",
    DEBUG="false",
    DATABASE_URL=f"sqlite:///{(_TMP_ROOT / 'test.db').as_posix()}",
    STORAGE_BACKEND="local",
    STORAGE_ROOT=str(_TMP_ROOT / "storage"),
    STORAGE_SIGNING_SECRET="test-signing-secret-at-least-32-bytes-long",
    JWT_SECRET="test-jwt-secret-at-least-32-bytes-long!!",
    QUEUE_BACKEND="thread",
    THREAD_QUEUE_WORKERS="1",
    # bcrypt at 12 rounds costs ~250ms per hash; the suite creates users constantly and the
    # cost factor is not what is under test.
    BCRYPT_ROUNDS="4",
    PUBLIC_BASE_URL="http://testserver",
    FRAME_INTERVAL_SECONDS="1",
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.enums import Role  # noqa: E402
from app.database.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _cleanup() -> Iterator[None]:
    yield
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_database() -> Iterator[None]:
    """A fresh schema for every test."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def storage_root() -> Path:
    return Path(os.environ["STORAGE_ROOT"])


# ---- user / auth helpers -----------------------------------------------------
PASSWORD = "test-password-1234"


def make_user(db: Session, email: str, role: Role, name: str | None = None) -> object:
    from app.auth.security import hash_password
    from app.models import User

    user = User(
        email=email,
        full_name=name or email.split("@")[0].title(),
        password_hash=hash_password(PASSWORD),
        role=role,
    )
    db.add(user)
    db.commit()
    return user


def auth_headers(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def admin(db: Session) -> object:
    return make_user(db, "admin@test.example.com", Role.ADMIN, "Test Admin")


@pytest.fixture
def manager(db: Session) -> object:
    return make_user(db, "pm@test.example.com", Role.PROJECT_MANAGER, "Test Manager")


@pytest.fixture
def engineer(db: Session) -> object:
    return make_user(db, "engineer@test.example.com", Role.SITE_ENGINEER, "Test Engineer")


@pytest.fixture
def viewer(db: Session) -> object:
    return make_user(db, "viewer@test.example.com", Role.VIEWER, "Test Viewer")


@pytest.fixture
def admin_headers(client: TestClient, admin: object) -> dict[str, str]:
    return auth_headers(client, "admin@test.example.com")


@pytest.fixture
def engineer_headers(client: TestClient, engineer: object) -> dict[str, str]:
    return auth_headers(client, "engineer@test.example.com")


@pytest.fixture
def viewer_headers(client: TestClient, viewer: object) -> dict[str, str]:
    return auth_headers(client, "viewer@test.example.com")


# ---- media helpers -----------------------------------------------------------
@pytest.fixture(scope="session")
def demo_room() -> object:
    from app.processing.synthetic import Room

    return Room()


@pytest.fixture(scope="session")
def panorama_factory(demo_room: object):
    """Render small synthetic panoramas, cached across the session.

    Rendering is the slowest thing in the suite, so identical requests are served from a cache.
    The images are still produced by the real renderer, not stubs.
    """
    from app.processing.media import encode_jpeg
    from app.processing.synthetic import render_panorama

    cache: dict[tuple, bytes] = {}

    def factory(
        position: tuple[float, float, float] = (0.0, 0.0, 0.0),
        *,
        # 2048×1024 is the smallest size the quality engine considers usable for construction
        # work: its resolution floor is 1024px, so a 1024-wide panorama is deliberately scored
        # `poor`. Tests that assert on realistic behaviour need a realistic input; tests about
        # low resolution pass a smaller size explicitly.
        width: int = 2048,
        height: int = 1024,
        brightness: float = 1.0,
        quality: int = 88,
    ) -> bytes:
        key = (position, width, height, brightness, quality)
        if key not in cache:
            image = render_panorama(
                demo_room, position, width=width, height=height, brightness=brightness
            )
            cache[key] = encode_jpeg(image, quality=quality)
        return cache[key]

    return factory
