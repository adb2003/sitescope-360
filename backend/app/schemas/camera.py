from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from app.core.enums import CameraTransport, CaptureSource, ProjectionType
from app.schemas.common import ORMModel


class CameraDeviceCreate(BaseModel):
    label: Annotated[str, Field(min_length=1, max_length=200)]
    make: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    provider_key: str = "local_upload"
    transport: CameraTransport = CameraTransport.FILE_UPLOAD
    capture_source: CaptureSource = CaptureSource.HUMAN_OPERATOR
    default_projection: ProjectionType = ProjectionType.EQUIRECTANGULAR
    native_width_px: int | None = None
    native_height_px: int | None = None
    config: dict[str, Any] | None = None
    notes: str | None = None


class CameraDeviceUpdate(BaseModel):
    label: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    is_active: bool | None = None
    config: dict[str, Any] | None = None
    notes: str | None = None


class CameraDeviceOut(ORMModel):
    id: str
    label: str
    make: str | None
    model: str | None
    serial_number: str | None
    provider_key: str
    transport: CameraTransport
    capture_source: CaptureSource
    default_projection: ProjectionType
    native_width_px: int | None
    native_height_px: int | None
    is_active: bool
    last_seen_at: datetime | None
    config: dict[str, Any] | None
    notes: str | None
    created_at: datetime


class CameraProviderInfo(BaseModel):
    """Describes one registered ``CameraProvider`` so the UI can render capabilities honestly."""

    key: str
    display_name: str
    transport: CameraTransport
    implemented: bool
    description: str
    supports_live_transfer: bool
    supports_remote_trigger: bool
    accepted_extensions: list[str]
    setup_hint: str | None = None
