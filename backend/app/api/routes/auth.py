"""Authentication routes (§27)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.api.deps import (
    DbSession,
    Pagination,
    RequireUserManage,
    UserSettingPassword,
)
from app.auth import ratelimit
from app.auth.rbac import capabilities_for
from app.auth.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.core.enums import Role
from app.core.errors import AuthenticationError, ConflictError, NotFoundError, ValidationError
from app.core.logging import log_event
from app.models import RefreshToken, User
from app.schemas.auth import (
    LoginRequest,
    PasswordChange,
    PasswordReset,
    RefreshRequest,
    SessionInfo,
    TokenPair,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.schemas.common import Message, Page

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, request: Request, db: DbSession) -> TokenPair:
    # Both checks come before the password is verified, and that ordering is the point: bcrypt is
    # deliberately expensive, so a limiter that runs afterwards has already paid the cost it
    # exists to avoid. See `app/auth/ratelimit.py` for why there are two of them.
    ratelimit.check_address(request)
    ratelimit.check_account(payload.email)

    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    # Verify against a dummy hash when the user does not exist, so a missing account and a wrong
    # password take the same time and cannot be distinguished by an attacker.
    if user is None:
        verify_password(payload.password, _timing_equaliser_hash())
        ratelimit.record_failure(payload.email)
        raise AuthenticationError("Incorrect email or password")
    if not verify_password(payload.password, user.password_hash):
        ratelimit.record_failure(payload.email)
        raise AuthenticationError("Incorrect email or password")
    if not user.is_active:
        # Not counted as a failure: the credentials were right, and counting this would let a
        # deactivated account's own correct attempts lock out the address it is signing in from.
        raise AuthenticationError("This account has been deactivated")

    # The correct password has been demonstrated, so earlier fumbles stop counting — otherwise a
    # user who mistypes four times and then succeeds spends the rest of the window one mistake
    # from a lockout, for no security benefit.
    ratelimit.clear_account(payload.email)
    user.last_login_at = datetime.now(UTC)
    tokens = _issue_tokens(db, user, request.headers.get("user-agent"))
    db.commit()
    log_event("auth.login", user_id=user.id, role=str(user.role))
    return tokens


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, request: Request, db: DbSession) -> TokenPair:
    """Rotate the refresh token: the presented token is revoked and a new one issued."""
    # Unauthenticated in the same sense login is — anyone can post a guess at a token — so it
    # gets the address limit too. No account limit: the caller has not named an account, and a
    # refresh token is 256 bits rather than something a person chose.
    ratelimit.check_address(request)
    digest = hash_refresh_token(payload.refresh_token)
    record = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == digest))
    if record is None or record.revoked_at is not None:
        raise AuthenticationError("Refresh token is invalid or has been used")
    if record.expires_at < datetime.now(UTC):
        raise AuthenticationError("Refresh token has expired")

    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is unavailable")

    record.revoked_at = datetime.now(UTC)
    tokens = _issue_tokens(db, user, request.headers.get("user-agent"))
    db.commit()
    return tokens


@router.post("/logout", response_model=Message)
def logout(payload: RefreshRequest, db: DbSession) -> Message:
    digest = hash_refresh_token(payload.refresh_token)
    record = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == digest))
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        db.commit()
    return Message(message="Signed out")


@router.get("/me", response_model=SessionInfo)
def me(user: UserSettingPassword) -> SessionInfo:
    """Who is signed in, and what their **account** may do.

    A ceiling, not an answer: a role is held on a project (§27), so this list says what the
    account could do somewhere, and `your_capabilities` on each project says what they can do
    there. An interface that gates project controls on this alone offers a manager's buttons on a
    job where the person is a viewer — the fields are named `your_*` on the project for exactly
    that reason, and the web client uses `useProjectCan` wherever a project is in hand. What this
    list is right for is the capabilities that have no project: `user:manage`, `settings:system`,
    `job:admin`, and creating a project in the first place.

    Reachable by an account held for a password reset, unlike everything else — the client has
    to be able to render a shell and read `user.must_change_password` to know where to send
    them. The capabilities are reported unchanged, because the role has not changed; what is
    withheld is the ability to act on them, and that is enforced per request rather than by
    editing this list into a half-truth.
    """
    return SessionInfo(
        user=UserOut.model_validate(user),
        capabilities=sorted(str(c) for c in capabilities_for(user.role)),
    )


@router.post("/change-password", response_model=Message)
def change_password(payload: PasswordChange, user: UserSettingPassword, db: DbSession) -> Message:
    """Set your own password, which is also the only way out of an administrative reset."""
    if not verify_password(payload.current_password, user.password_hash):
        raise AuthenticationError("Current password is incorrect")
    if payload.new_password == payload.current_password:
        # Otherwise a held account could satisfy the requirement with the password the
        # administrator chose, which is the one thing the hold exists to prevent.
        raise ValidationError("The new password must differ from the current one")
    user.password_hash = hash_password(payload.new_password)
    # Whatever brought them here, this password is now theirs.
    user.must_change_password = False
    # Changing a password invalidates every existing session.
    for token in db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ):
        token.revoked_at = datetime.now(UTC)
    db.commit()
    # No "was this a reset" field: this route is only ever self-service, so it could only
    # ever say False. `auth.password_reset` is the event for the other case.
    log_event("auth.password_changed", user_id=user.id, actor_id=user.id)
    return Message(message="Password updated. Please sign in again on other devices.")


# ---- user administration -----------------------------------------------------
users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("", response_model=Page[UserOut])
def list_users(db: DbSession, page: Pagination, _: RequireUserManage) -> Page[UserOut]:
    from sqlalchemy import func

    total = int(db.scalar(select(func.count(User.id))) or 0)
    rows = db.scalars(select(User).order_by(User.full_name).limit(page.limit).offset(page.offset))
    return Page(
        items=[UserOut.model_validate(row) for row in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@users_router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: DbSession, _: RequireUserManage) -> UserOut:
    email = payload.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise ConflictError(f"A user with the email {email} already exists")
    user = User(
        email=email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    log_event("auth.user_created", user_id=user.id, role=str(user.role))
    return UserOut.model_validate(user)


@users_router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: str, payload: UserUpdate, db: DbSession, actor: RequireUserManage
) -> UserOut:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")

    # Guard against an administrator locking themselves — and possibly everyone — out.
    if user.id == actor.id and payload.role is not None and payload.role != Role.ADMIN:
        raise ValidationError("You cannot remove your own admin role")
    if user.id == actor.id and payload.is_active is False:
        raise ValidationError("You cannot deactivate your own account")

    was_role, was_active = user.role, user.is_active
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    db.commit()

    # A role change is a privilege change, and it left no trace at all until now — which meant
    # the question "who gave this account admin, and when" had no answer. Logged with both
    # sides, because the previous role is the part a reader cannot reconstruct afterwards.
    if payload.role is not None and payload.role != was_role:
        log_event(
            "auth.role_changed",
            user_id=user.id,
            actor_id=actor.id,
            from_role=str(was_role),
            to_role=str(user.role),
        )
    # Two calls rather than one with a conditional name. `log_event` exists so the observable
    # event names are literals a grep can find; choosing the name in an expression defeats
    # exactly that, and the registry guard in `tests/unit/test_audit_events.py` sees it as an
    # event promised and never emitted.
    if payload.is_active is not None and payload.is_active != was_active:
        if user.is_active:
            log_event("auth.user_activated", user_id=user.id, actor_id=actor.id)
        else:
            log_event("auth.user_deactivated", user_id=user.id, actor_id=actor.id)
    return UserOut.model_validate(user)


@users_router.delete("/{user_id}", response_model=Message)
def deactivate_user(user_id: str, db: DbSession, actor: RequireUserManage) -> Message:
    """Deactivates rather than deletes: capture sessions must keep their operator attribution."""
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    if user.id == actor.id:
        raise ValidationError("You cannot deactivate your own account")
    user.is_active = False
    revoked = _revoke_sessions(db, user)
    db.commit()
    log_event("auth.user_deactivated", user_id=user.id, actor_id=actor.id, sessions_revoked=revoked)
    return Message(message=f"{user.email} deactivated")


@users_router.post("/{user_id}/password", response_model=Message)
def reset_user_password(
    user_id: str, payload: PasswordReset, db: DbSession, actor: RequireUserManage
) -> Message:
    """Set another account's password, for the person who has forgotten theirs.

    There is no self-service reset and there cannot be one here: proving you own an address
    means sending something to it, and nothing in this platform may depend on a mail service to
    run locally. So recovery is an administrator action, and the honest way to do that is to
    make the credential they set explicitly temporary — `must_change_password` holds the account
    to setting its own password and nothing else, so a password somebody else chose cannot
    quietly become the one protecting the account.

    Every existing session is revoked. A reset is what you do when you suspect the account is
    reachable by somebody who should not reach it, and leaving their refresh token alive would
    answer the wrong half of the problem.
    """
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    if user.id == actor.id:
        # Not a refusal about safety — about honesty. Resetting your own password through this
        # route would hold your own account and skip the current-password check that
        # `/auth/change-password` exists to make.
        raise ValidationError("Use change-password to set your own password")

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = True
    revoked = _revoke_sessions(db, user)
    db.commit()
    log_event(
        "auth.password_reset",
        user_id=user.id,
        actor_id=actor.id,
        sessions_revoked=revoked,
    )
    # The password is not echoed. The administrator typed it; repeating it here would put it in
    # every response log between them and the browser for no gain.
    return Message(
        message=(
            f"Password reset for {user.email}. They must set their own password at next sign-in, "
            f"and {revoked} existing session(s) were signed out."
        )
    )


def _revoke_sessions(db: DbSession, user: User) -> int:
    """End every live session for one account. Returns how many there were."""
    count = 0
    for token in db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ):
        token.revoked_at = datetime.now(UTC)
        count += 1
    return count


# ---- helpers -----------------------------------------------------------------
#: A real bcrypt hash of a random value, used only to equalise login timing. Computed lazily so
#: the ~250ms bcrypt cost is not paid at import time, and cached so it is paid at most once.
_dummy_hash: str | None = None


def _timing_equaliser_hash() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(24))
    return _dummy_hash


def _issue_tokens(db: DbSession, user: User, user_agent: str | None) -> TokenPair:
    access_token, expires_at = create_access_token(
        user_id=user.id, email=user.email, role=user.role
    )
    plaintext, digest, refresh_expires = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=digest,
            expires_at=refresh_expires,
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    return TokenPair(access_token=access_token, refresh_token=plaintext, expires_at=expires_at)
