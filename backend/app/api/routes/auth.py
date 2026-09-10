"""Authentication routes (§27)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, Pagination, RequireUserManage
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
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    # Verify against a dummy hash when the user does not exist, so a missing account and a wrong
    # password take the same time and cannot be distinguished by an attacker.
    if user is None:
        verify_password(payload.password, _timing_equaliser_hash())
        raise AuthenticationError("Incorrect email or password")
    if not verify_password(payload.password, user.password_hash):
        raise AuthenticationError("Incorrect email or password")
    if not user.is_active:
        raise AuthenticationError("This account has been deactivated")

    user.last_login_at = datetime.now(UTC)
    tokens = _issue_tokens(db, user, request.headers.get("user-agent"))
    db.commit()
    log_event("auth.login", user_id=user.id, role=str(user.role))
    return tokens


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, request: Request, db: DbSession) -> TokenPair:
    """Rotate the refresh token: the presented token is revoked and a new one issued."""
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
def me(user: CurrentUser) -> SessionInfo:
    return SessionInfo(
        user=UserOut.model_validate(user),
        capabilities=sorted(str(c) for c in capabilities_for(user.role)),
    )


@router.post("/change-password", response_model=Message)
def change_password(payload: PasswordChange, user: CurrentUser, db: DbSession) -> Message:
    if not verify_password(payload.current_password, user.password_hash):
        raise AuthenticationError("Current password is incorrect")
    user.password_hash = hash_password(payload.new_password)
    # Changing a password invalidates every existing session.
    for token in db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ):
        token.revoked_at = datetime.now(UTC)
    db.commit()
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

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    db.commit()
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
    for token in db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ):
        token.revoked_at = datetime.now(UTC)
    db.commit()
    return Message(message=f"{user.email} deactivated")


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
