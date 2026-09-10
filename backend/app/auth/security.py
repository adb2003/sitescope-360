"""Password hashing and JWT issue/verify (§27, §44)."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.core.config import settings
from app.core.enums import Role
from app.core.errors import AuthenticationError

_BCRYPT_MAX_BYTES = 72


# ---- passwords ---------------------------------------------------------------
def hash_password(password: str) -> str:
    # bcrypt silently truncates at 72 bytes; pre-hashing keeps long passphrases fully significant.
    material = _prehash(password)
    return bcrypt.hashpw(material, bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode())
    except (ValueError, TypeError):
        return False


def _prehash(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) <= _BCRYPT_MAX_BYTES:
        return raw
    return hashlib.sha256(raw).hexdigest().encode("ascii")


# ---- tokens ------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: str
    role: Role
    email: str
    expires_at: datetime
    token_type: str


def create_access_token(*, user_id: str, email: str, role: Role) -> tuple[str, datetime]:
    expires = datetime.now(UTC) + timedelta(minutes=settings.access_token_ttl_minutes)
    payload: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "role": str(role),
        "type": "access",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires


def decode_access_token(token: str) -> TokenClaims:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Access token has expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid access token") from exc

    if payload.get("type") != "access":
        raise AuthenticationError("Wrong token type")
    try:
        role = Role(payload["role"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Invalid token role") from exc

    return TokenClaims(
        subject=str(payload["sub"]),
        role=role,
        email=str(payload.get("email", "")),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        token_type="access",
    )


def new_refresh_token() -> tuple[str, str, datetime]:
    """Return ``(plaintext, sha256_hex, expires_at)``.

    Only the hash is persisted, so a database dump cannot be replayed as a login.
    """
    plaintext = secrets.token_urlsafe(48)
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    expires = datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days)
    return plaintext, digest, expires


def hash_refresh_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()
