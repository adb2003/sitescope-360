"""Declarative base and portable column types.

The production target is PostgreSQL; SQLite is supported for local development. To keep one set of
models working on both, dialect-specific types are chosen at DDL-compile time:

* ``JSONType``  → ``JSONB`` on PostgreSQL, ``JSON`` elsewhere.
* ``PKType``    → ``VARCHAR(32)`` everywhere (UUID4 hex), so no dialect UUID support is required.
* Timestamps are stored timezone-aware; SQLite gets naive UTC and is normalised on read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, String, TypeDecorator, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.ids import new_id


class Base(DeclarativeBase):
    pass


class JSONType(TypeDecorator):
    """JSONB on PostgreSQL, plain JSON elsewhere."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class EnumType(TypeDecorator):
    """Persist a :class:`~enum.StrEnum` as its string value, and return the member on load.

    Without this, a ``Mapped[JobStatus]`` column hands back a plain ``str``. That makes the type
    annotation untrue and, worse, silently breaks identity comparisons: ``job.status is
    JobStatus.COMPLETE`` is ``False`` for the string ``"complete"`` even though ``==`` is
    ``True``. Fixing it at the type keeps the models honest and means call sites can compare
    however reads best.

    A native database ENUM is deliberately not used: adding a value to a PostgreSQL enum requires
    a migration and cannot happen inside a transaction with other DDL, whereas a VARCHAR with
    validation in the application layer evolves freely. The DDL is a plain ``VARCHAR(n)``.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[Any], length: int = 32, **kwargs: Any) -> None:
        self.enum_class = enum_class
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return str(value.value)
        # Accept a plain string, but validate it — a typo should fail on write, not read.
        return str(self.enum_class(value).value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        try:
            return self.enum_class(value)
        except ValueError:
            # A value written by a newer version of the application, or by hand. Returning it
            # raw keeps reads working instead of failing an entire query on one bad row.
            return value


class UTCDateTime(TypeDecorator):
    """Always hand Python a timezone-aware UTC datetime, whatever the backend stored."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


PKType = String(32)


def pk_column() -> Mapped[str]:
    return mapped_column(PKType, primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


def utcnow() -> datetime:
    return datetime.now(UTC)
