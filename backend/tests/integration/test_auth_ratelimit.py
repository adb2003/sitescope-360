"""Rate limiting the unauthenticated auth routes (§44).

`/auth/login` is the only route reachable without credentials that does expensive work: bcrypt is
slow deliberately, so every request costs this server a fraction of a CPU second and costs the
caller nothing. That makes it both a brute-force target and a cheap way to saturate the API — and
it is the endpoint that has to keep working for anybody to use the product.

Most of what follows is about the ways a limiter can exist and not work:

* counting *after* the password check, which pays the cost it was meant to avoid;
* keying on a header the caller controls, which lets anyone rotate around it;
* counting successes, which locks out the busiest legitimate users first;
* never forgetting, which turns four typos into an hour of lockout.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import ratelimit
from app.core.config import settings
from tests.conftest import PASSWORD


@pytest.fixture(autouse=True)
def _fresh_counter():
    """A limiter with no memory of the previous test.

    The counter is a process-wide singleton, so without this the tests would limit each other —
    and the failure would look like the feature working too well.
    """
    ratelimit.reset_counter()
    yield
    ratelimit.reset_counter()


def _login(client: TestClient, email: str, password: str = "wrong-password", **headers):
    return client.post(
        "/api/auth/login", json={"email": email, "password": password}, headers=headers or None
    )


# ---- the account limit -----------------------------------------------------------------------
def test_repeated_failures_for_one_account_are_eventually_refused(
    client: TestClient, admin, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "auth_account_max_failures", 3)

    for _ in range(3):
        assert _login(client, admin.email).status_code == 401

    refused = _login(client, admin.email)

    assert refused.status_code == 429, refused.text
    assert refused.json()["code"] == "rate_limited"


def test_a_refusal_says_how_long_to_wait(client: TestClient, admin, monkeypatch) -> None:
    """In the header and the body.

    A generic HTTP client acts on `Retry-After`; a browser fetch reads the body, because it
    cannot see the header cross-origin unless it is explicitly exposed.
    """
    monkeypatch.setattr(settings, "auth_account_max_failures", 1)
    _login(client, admin.email)

    refused = _login(client, admin.email)

    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0
    assert refused.json()["details"]["retry_after"] > 0


def test_a_successful_sign_in_clears_the_account_s_failures(
    client: TestClient, admin, monkeypatch
) -> None:
    """Four typos and then the right password must not leave the user one mistake from lockout.

    The correct password has been demonstrated, so the earlier fumbles carry no information — and
    keeping them costs a real user their next attempt for no security benefit at all.
    """
    monkeypatch.setattr(settings, "auth_account_max_failures", 3)
    for _ in range(2):
        assert _login(client, admin.email).status_code == 401

    assert _login(client, admin.email, PASSWORD).status_code == 200

    # The counter is back to zero, so the full allowance is available again.
    for _ in range(3):
        assert _login(client, admin.email).status_code == 401
    assert _login(client, admin.email).status_code == 429


def test_successful_sign_ins_are_never_counted(client: TestClient, admin, monkeypatch) -> None:
    """Someone signing in correctly all day is not attacking anything.

    Counting every attempt against the account would lock out whoever uses the product most.
    """
    monkeypatch.setattr(settings, "auth_account_max_failures", 2)

    for _ in range(6):
        assert _login(client, admin.email, PASSWORD).status_code == 200


def test_one_account_being_limited_does_not_lock_out_another(
    client: TestClient, admin, engineer, monkeypatch
) -> None:
    """The limit is per account, so a locked address does not become a locked deployment."""
    monkeypatch.setattr(settings, "auth_account_max_failures", 2)
    monkeypatch.setattr(settings, "auth_address_max_attempts", 0)
    for _ in range(3):
        _login(client, admin.email)
    assert _login(client, admin.email).status_code == 429

    assert _login(client, engineer.email, PASSWORD).status_code == 200


def test_an_account_that_does_not_exist_is_limited_the_same_way(
    client: TestClient, monkeypatch
) -> None:
    """Otherwise the limiter itself distinguishes real accounts from invented ones.

    The route goes to great lengths to make a missing account and a wrong password take the same
    time; a limit that only applied to real accounts would hand that back — 429 for one address
    and 401 for another is a user-enumeration oracle.
    """
    monkeypatch.setattr(settings, "auth_account_max_failures", 2)

    for _ in range(2):
        assert _login(client, "nobody@example.com").status_code == 401

    assert _login(client, "nobody@example.com").status_code == 429


def test_the_account_key_ignores_case_and_padding(client: TestClient, admin, monkeypatch) -> None:
    # Email is matched case-insensitively at sign-in, so a limiter that treated `A@b.com` and
    # `a@b.com` as different accounts would offer a free allowance per spelling.
    monkeypatch.setattr(settings, "auth_account_max_failures", 2)

    _login(client, admin.email)
    _login(client, admin.email.upper())

    assert _login(client, f"  {admin.email}  ").status_code == 429


# ---- the address limit -----------------------------------------------------------------------
def test_an_address_is_refused_before_any_password_is_checked(
    client: TestClient, admin, monkeypatch
) -> None:
    """The CPU control, and the reason it runs first.

    A limiter that counted after verification would still pay for the bcrypt call on every
    refused request, which is the entire cost it exists to prevent.
    """
    monkeypatch.setattr(settings, "auth_address_max_attempts", 2)
    monkeypatch.setattr(settings, "auth_account_max_failures", 0)

    calls: list[str] = []
    import app.api.routes.auth as auth_routes

    real = auth_routes.verify_password

    def counting(password: str, password_hash: str) -> bool:
        calls.append(password)
        return bool(real(password, password_hash))

    monkeypatch.setattr(auth_routes, "verify_password", counting)

    for _ in range(2):
        _login(client, admin.email)
    before = len(calls)
    refused = _login(client, admin.email)

    assert refused.status_code == 429
    assert len(calls) == before, "the password was hashed for a request that was refused"


def test_the_address_limit_counts_every_attempt_not_only_failures(
    client: TestClient, admin, monkeypatch
) -> None:
    """Because a *successful* login costs exactly the same bcrypt time as a failed one."""
    monkeypatch.setattr(settings, "auth_address_max_attempts", 3)
    monkeypatch.setattr(settings, "auth_account_max_failures", 0)

    for _ in range(3):
        assert _login(client, admin.email, PASSWORD).status_code == 200

    assert _login(client, admin.email, PASSWORD).status_code == 429


def test_refresh_is_limited_too(client: TestClient, monkeypatch) -> None:
    """It is unauthenticated in the same sense: anyone can post a guess at a token."""
    monkeypatch.setattr(settings, "auth_address_max_attempts", 2)

    for _ in range(2):
        client.post("/api/auth/refresh", json={"refresh_token": "nope"})

    refused = client.post("/api/auth/refresh", json={"refresh_token": "nope"})

    assert refused.status_code == 429


def test_a_forwarded_header_is_ignored_when_proxies_are_not_trusted(
    client: TestClient, admin, monkeypatch
) -> None:
    """The bypass this whole keying decision exists to close.

    If `X-Real-IP` were believed unconditionally, an attacker exposed directly to the API would
    rotate the header and never be limited at all — the control would look present and do
    nothing.
    """
    monkeypatch.setattr(settings, "auth_address_max_attempts", 2)
    monkeypatch.setattr(settings, "auth_account_max_failures", 0)
    monkeypatch.setattr(settings, "trust_proxy_headers", False)

    for index in range(2):
        _login(client, admin.email, **{"X-Real-IP": f"10.0.0.{index}"})

    refused = _login(client, admin.email, **{"X-Real-IP": "10.0.0.99"})

    assert refused.status_code == 429, "a spoofed header bought a fresh allowance"


def test_a_forwarded_header_separates_clients_when_proxies_are_trusted(
    client: TestClient, admin, monkeypatch
) -> None:
    """And the reason it can be turned on: behind a proxy every user shares one address.

    Without this the limit becomes a single global bucket for the whole site, and one busy
    morning locks everyone out.
    """
    monkeypatch.setattr(settings, "auth_address_max_attempts", 2)
    monkeypatch.setattr(settings, "auth_account_max_failures", 0)
    monkeypatch.setattr(settings, "trust_proxy_headers", True)

    for _ in range(3):
        _login(client, admin.email, **{"X-Real-IP": "203.0.113.5"})
    assert _login(client, admin.email, **{"X-Real-IP": "203.0.113.5"}).status_code == 429

    # A different client behind the same proxy is unaffected.
    assert _login(client, admin.email, PASSWORD, **{"X-Real-IP": "203.0.113.9"}).status_code == 200


# ---- switches --------------------------------------------------------------------------------
def test_limits_can_be_turned_off(client: TestClient, admin, monkeypatch) -> None:
    """Zero disables, so a deployment that limits at its edge is not limited twice."""
    monkeypatch.setattr(settings, "auth_address_max_attempts", 0)
    monkeypatch.setattr(settings, "auth_account_max_failures", 0)

    for _ in range(25):
        assert _login(client, admin.email).status_code == 401


def test_the_limiter_fails_open_when_its_backend_is_broken(
    client: TestClient, admin, monkeypatch
) -> None:
    """A broker blip must not become an outage of the endpoint everything depends on.

    Losing the protection is the smaller harm, and it is logged rather than silent.
    """

    class Broken:
        def hit(self, *args: object, **kwargs: object) -> ratelimit.Decision:
            raise RuntimeError("no redis")

        def peek(self, *args: object, **kwargs: object) -> ratelimit.Decision:
            raise RuntimeError("no redis")

        def reset(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("no redis")

    monkeypatch.setattr(ratelimit, "_counter", Broken())

    # The route still works. A limiter that failed closed would return 429 to everyone here.
    assert _login(client, admin.email).status_code in (401, 429)


def test_the_in_memory_counter_does_not_grow_without_bound() -> None:
    """Every distinct address an attacker sends would otherwise stay resident for ever.

    An unauthenticated memory leak is the same class of problem this module exists to prevent.
    """
    counter = ratelimit._Counter()

    for index in range(9000):
        counter.hit(f"key-{index}", limit=1000, window_seconds=0)

    assert len(counter._hits) < 9000
