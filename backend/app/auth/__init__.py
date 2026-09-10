"""Authentication and role-based authorization (§27)."""

from app.auth.rbac import Capability, capabilities_for, has_capability, role_at_least
from app.auth.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

__all__ = [
    "Capability",
    "capabilities_for",
    "create_access_token",
    "decode_access_token",
    "has_capability",
    "hash_password",
    "role_at_least",
    "verify_password",
]
