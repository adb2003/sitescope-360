"""Registered capture devices (§6, §43, §47).

A ``CameraDevice`` row is a *registration*, not a driver. The driver is a ``CameraProvider``
resolved by ``provider_key`` at runtime (see ``app/cameras/``), so adding hardware support never
requires a migration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import CameraTransport, CaptureSource, ProjectionType
from app.database.base import (
    Base,
    EnumType,
    JSONType,
    PKType,
    TimestampMixin,
    UTCDateTime,
    pk_column,
)


class CameraDevice(Base, TimestampMixin):
    __tablename__ = "camera_devices"

    id: Mapped[str] = pk_column()
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    make: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: Key into the CameraProvider registry, e.g. ``local_upload``, ``folder_import``, ``osc``.
    provider_key: Mapped[str] = mapped_column(String(60), nullable=False, default="local_upload")
    transport: Mapped[CameraTransport] = mapped_column(
        EnumType(CameraTransport, 32), nullable=False, default=CameraTransport.FILE_UPLOAD
    )
    capture_source: Mapped[CaptureSource] = mapped_column(
        EnumType(CaptureSource, 32), nullable=False, default=CaptureSource.HUMAN_OPERATOR
    )

    default_projection: Mapped[ProjectionType] = mapped_column(
        EnumType(ProjectionType, 32), nullable=False, default=ProjectionType.EQUIRECTANGULAR
    )
    native_width_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    native_height_px: Mapped[int | None] = mapped_column(Integer, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: Provider-specific connection settings (host, port, folder path, …). Never secrets.
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
