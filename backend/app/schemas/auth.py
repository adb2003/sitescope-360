from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field

from app.core.enums import Role
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=200)]


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: datetime


class UserOut(ORMModel):
    id: str
    email: str
    full_name: str
    role: Role
    is_active: bool
    #: True while an administrator's reset is outstanding: the account can reach `/auth/me`
    #: and `/auth/change-password` and nothing else until it sets its own password.
    must_change_password: bool = False
    last_login_at: datetime | None = None
    created_at: datetime


class UserCreate(BaseModel):
    email: EmailStr
    full_name: Annotated[str, Field(min_length=1, max_length=200)]
    password: Annotated[str, Field(min_length=10, max_length=200)]
    role: Role = Role.VIEWER


class UserUpdate(BaseModel):
    full_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    role: Role | None = None
    is_active: bool | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: Annotated[str, Field(min_length=10, max_length=200)]


class PasswordReset(BaseModel):
    """An administrator setting a temporary password for somebody who has lost theirs.

    Same length floor as every other password here. It is temporary by policy rather than by
    construction — nothing can stop an administrator choosing a weak one — so the account is
    held to replacing it rather than trusted to.
    """

    new_password: Annotated[str, Field(min_length=10, max_length=200)]


class SessionInfo(BaseModel):
    user: UserOut
    capabilities: list[str]
