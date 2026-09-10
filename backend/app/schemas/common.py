"""Shared schema building blocks."""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """Offset pagination envelope. Every list endpoint returns this (§35)."""

    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class PageParams(BaseModel):
    limit: Annotated[int, Field(ge=1, le=200)] = 50
    offset: Annotated[int, Field(ge=0)] = 0


class Message(BaseModel):
    message: str


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict[str, object] | None = None


class SignedAsset(BaseModel):
    """A storage object exposed to the client as a time-limited URL, never a path (§44)."""

    key: str
    url: str
    expires_in: int
