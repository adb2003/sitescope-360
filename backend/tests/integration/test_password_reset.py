"""Recovering an account whose password is gone, without lying about what happened (§27, §46).

There was no way. `POST /auth/change-password` needs the current password and an administrator
could change a user's name, role and active flag but not their credential — so the most ordinary
support request there is required somebody with database access.

Self-service is genuinely impossible here and these tests do not pretend otherwise: proving you
own an address means sending something to it, and nothing in this project may depend on a mail
service to run locally. So recovery is an administrator action, and the whole question is whether
the temporary credential they set stays temporary. That is what most of this file is about.

The test that matters most is `test_a_held_account_cannot_do_anything_else`: without it, a
"reset" that merely set a password would hand out a working account protected by a secret
somebody else chose and very possibly said out loud.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.enums import Role
from tests.conftest import PASSWORD, auth_headers, make_user

TEMPORARY = "temp-password-4821"
CHOSEN = "their-own-password-9931"


def _token(client: TestClient, email: str, password: str):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def _bearer(response) -> dict[str, str]:
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _reset(client: TestClient, admin_headers: dict[str, str], user_id: str, password=TEMPORARY):
    return client.post(
        f"/api/users/{user_id}/password", json={"new_password": password}, headers=admin_headers
    )


# ---- the capability ----------------------------------------------------------
def test_an_administrator_can_give_a_locked_out_user_a_way_back_in(
    client: TestClient, db, admin, admin_headers
) -> None:
    """The feature: somebody forgot their password and is not stuck forever."""
    locked = make_user(db, "forgot@test.example.com", Role.SITE_ENGINEER)

    assert _reset(client, admin_headers, locked.id).status_code == 200

    signed_in = _token(client, locked.email, TEMPORARY)
    assert signed_in.status_code == 200, signed_in.text


def test_the_temporary_password_is_not_echoed_back(
    client: TestClient, db, admin, admin_headers
) -> None:
    """The administrator typed it; repeating it puts it in every log between here and them."""
    locked = make_user(db, "quiet@test.example.com", Role.VIEWER)

    body = _reset(client, admin_headers, locked.id).text

    assert TEMPORARY not in body


# ---- the hold ----------------------------------------------------------------
def test_a_held_account_cannot_do_anything_else(
    client: TestClient, db, admin, admin_headers
) -> None:
    """A reset password is a secret somebody else knows. It must not protect a working account.

    The refusal is a 403 with its own code rather than a 401: the token is genuine, so a client
    that treats it as an expiry would refresh in a loop and never reach the screen it needs.
    """
    locked = make_user(db, "held@test.example.com", Role.SITE_ENGINEER)
    _reset(client, admin_headers, locked.id)
    headers = _bearer(_token(client, locked.email, TEMPORARY))

    for path in ("/api/projects", "/api/captures", "/api/dashboard"):
        refused = client.get(path, headers=headers)
        assert refused.status_code == 403, f"{path} allowed a held account: {refused.text}"
        assert refused.json()["code"] == "password_change_required", refused.text


def test_a_held_account_can_still_see_who_it_is_and_set_a_password(
    client: TestClient, db, admin, admin_headers
) -> None:
    """Two endpoints stay open, because a client that cannot read them cannot recover."""
    locked = make_user(db, "recover@test.example.com", Role.SITE_ENGINEER)
    _reset(client, admin_headers, locked.id)
    headers = _bearer(_token(client, locked.email, TEMPORARY))

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["user"]["must_change_password"] is True

    changed = client.post(
        "/api/auth/change-password",
        json={"current_password": TEMPORARY, "new_password": CHOSEN},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text


def test_setting_a_password_lifts_the_hold(client: TestClient, db, admin, admin_headers) -> None:
    locked = make_user(db, "freed@test.example.com", Role.SITE_ENGINEER)
    _reset(client, admin_headers, locked.id)
    headers = _bearer(_token(client, locked.email, TEMPORARY))
    client.post(
        "/api/auth/change-password",
        json={"current_password": TEMPORARY, "new_password": CHOSEN},
        headers=headers,
    )

    # A fresh sign-in with their own password, and the platform is theirs again.
    after = _token(client, locked.email, CHOSEN)
    assert after.status_code == 200, after.text
    assert client.get("/api/projects", headers=_bearer(after)).status_code == 200
    assert (
        client.get("/api/auth/me", headers=_bearer(after)).json()["user"]["must_change_password"]
        is False
    )


def test_the_hold_cannot_be_satisfied_by_keeping_the_administrators_password(
    client: TestClient, db, admin, admin_headers
) -> None:
    """Otherwise the one thing the hold exists to prevent is a valid way out of it."""
    locked = make_user(db, "lazy@test.example.com", Role.SITE_ENGINEER)
    _reset(client, admin_headers, locked.id)
    headers = _bearer(_token(client, locked.email, TEMPORARY))

    refused = client.post(
        "/api/auth/change-password",
        json={"current_password": TEMPORARY, "new_password": TEMPORARY},
        headers=headers,
    )

    assert refused.status_code == 422, refused.text
    assert "differ" in refused.text
    assert client.get("/api/projects", headers=headers).status_code == 403


def test_the_hold_applies_to_a_session_opened_before_the_reset(
    client: TestClient, db, admin, admin_headers
) -> None:
    """Checked against the row, not written into the token, so nothing outlives it.

    This is the case a login-time check would miss entirely: the person whose account is being
    reset because somebody else may be in it is precisely the person already holding a token.
    """
    user = make_user(db, "midsession@test.example.com", Role.SITE_ENGINEER)
    before = _bearer(_token(client, user.email, PASSWORD))
    assert client.get("/api/projects", headers=before).status_code == 200

    _reset(client, admin_headers, user.id)

    assert client.get("/api/projects", headers=before).status_code == 403


def test_a_reset_signs_out_every_existing_session(
    client: TestClient, db, admin, admin_headers
) -> None:
    """A reset answers "somebody may be in this account". A live refresh token is the other half."""
    user = make_user(db, "sessions@test.example.com", Role.SITE_ENGINEER)
    refresh = _token(client, user.email, PASSWORD).json()["refresh_token"]

    message = _reset(client, admin_headers, user.id).json()["message"]

    assert "1 existing session" in message, message
    refreshed = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert refreshed.status_code == 401, refreshed.text


# ---- refusals ----------------------------------------------------------------
def test_only_a_user_manager_can_reset_a_password(client: TestClient, db, engineer, admin) -> None:
    victim = make_user(db, "target@test.example.com", Role.VIEWER)

    refused = client.post(
        f"/api/users/{victim.id}/password",
        json={"new_password": TEMPORARY},
        headers=auth_headers(client, engineer.email),
    )

    assert refused.status_code == 403, refused.text


def test_an_administrator_cannot_reset_their_own_password_this_way(
    client: TestClient, admin, admin_headers
) -> None:
    """Not about safety — about honesty. It would skip the current-password check and self-hold."""
    refused = _reset(client, admin_headers, admin.id)

    assert refused.status_code == 422, refused.text
    assert "change-password" in refused.text


def test_a_short_temporary_password_is_refused(
    client: TestClient, db, admin, admin_headers
) -> None:
    """Temporary by policy, not by construction — but not an excuse to weaken the floor."""
    locked = make_user(db, "short@test.example.com", Role.VIEWER)

    refused = _reset(client, admin_headers, locked.id, password="short")

    assert refused.status_code == 422, refused.text


def test_resetting_an_unknown_user_is_a_404(client: TestClient, admin, admin_headers) -> None:
    assert _reset(client, admin_headers, "0" * 32).status_code == 404
