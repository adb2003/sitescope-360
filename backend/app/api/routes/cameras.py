"""Camera device registration and provider capability discovery (§6, §43)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import select

from app.api.deps import (
    DbSession,
    RequireCaptureRead,
    RequireSystemSettings,
    RequireUpload,
    get_session_or_404,
)
from app.cameras import UnsupportedOperation, get_provider, selectable_providers
from app.core.errors import NotFoundError, ValidationError
from app.models import CameraDevice
from app.schemas.camera import (
    CameraDeviceCreate,
    CameraDeviceOut,
    CameraDeviceUpdate,
    CameraProviderInfo,
)
from app.schemas.capture import CaptureFileOut
from app.schemas.common import Message
from app.services import captures as capture_service

router = APIRouter(tags=["cameras"])


@router.get("/cameras/providers", response_model=list[CameraProviderInfo])
def list_camera_providers(_: RequireCaptureRead) -> list[CameraProviderInfo]:
    """What the platform can talk to, and — just as importantly — what it cannot (§46)."""
    return [
        CameraProviderInfo(
            key=provider.capabilities.key,
            display_name=provider.capabilities.display_name,
            transport=provider.capabilities.transport,
            implemented=provider.capabilities.implemented,
            description=provider.capabilities.description,
            supports_live_transfer=provider.capabilities.supports_live_transfer,
            supports_remote_trigger=provider.capabilities.supports_remote_trigger,
            accepted_extensions=list(provider.capabilities.accepted_extensions),
            setup_hint=provider.capabilities.setup_hint,
        )
        for provider in selectable_providers()
    ]


@router.post("/cameras", response_model=CameraDeviceOut, status_code=status.HTTP_201_CREATED)
def register_camera(
    payload: CameraDeviceCreate, db: DbSession, user: RequireSystemSettings
) -> CameraDeviceOut:
    try:
        get_provider(payload.provider_key)
    except UnsupportedOperation as exc:
        raise ValidationError(str(exc)) from exc

    device = CameraDevice(**payload.model_dump(), owner_id=user.id)
    db.add(device)
    db.commit()
    return CameraDeviceOut.model_validate(device)


@router.get("/cameras", response_model=list[CameraDeviceOut])
def list_cameras(
    db: DbSession,
    _: RequireCaptureRead,
    active_only: Annotated[bool, Query()] = True,
) -> list[CameraDeviceOut]:
    query = select(CameraDevice)
    if active_only:
        query = query.where(CameraDevice.is_active.is_(True))
    rows = db.scalars(query.order_by(CameraDevice.label))
    return [CameraDeviceOut.model_validate(row) for row in rows]


@router.patch("/cameras/{device_id}", response_model=CameraDeviceOut)
def update_camera(
    device_id: str, payload: CameraDeviceUpdate, db: DbSession, _: RequireSystemSettings
) -> CameraDeviceOut:
    device = _get_or_404(db, device_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(device, field, value)
    db.commit()
    return CameraDeviceOut.model_validate(device)


@router.delete("/cameras/{device_id}", response_model=Message)
def deactivate_camera(device_id: str, db: DbSession, _: RequireSystemSettings) -> Message:
    device = _get_or_404(db, device_id)
    device.is_active = False
    db.commit()
    return Message(message=f"{device.label} deactivated")


@router.get("/cameras/{device_id}/health")
def camera_health(device_id: str, db: DbSession, _: RequireCaptureRead) -> dict:
    device = _get_or_404(db, device_id)
    provider = get_provider(device.provider_key)
    result = provider.health(device.config or {})
    if result.get("reachable"):
        device.last_seen_at = datetime.now(UTC)
        db.commit()
    return result


@router.get("/cameras/{device_id}/discover")
def discover_camera_files(device_id: str, db: DbSession, _: RequireUpload) -> dict:
    """List files available for import from a registered device."""
    device = _get_or_404(db, device_id)
    provider = get_provider(device.provider_key)
    try:
        files = [
            {"filename": item.filename, "size_bytes": item.size_bytes}
            for item in provider.discover(device.config or {})
        ]
    except UnsupportedOperation as exc:
        raise ValidationError(str(exc)) from exc
    return {
        "device_id": device.id,
        "provider": device.provider_key,
        "file_count": len(files),
        "files": files[:500],
    }


@router.post(
    "/captures/{session_id}/import",
    response_model=list[CaptureFileOut],
    status_code=status.HTTP_201_CREATED,
)
def import_from_camera(
    session_id: str,
    db: DbSession,
    _: RequireUpload,
    device_id: Annotated[str, Query(description="Registered camera device to import from")],
    filenames: Annotated[
        list[str] | None, Query(description="Specific files; omit to import everything found")
    ] = None,
) -> list[CaptureFileOut]:
    """Import files into a capture session from a registered device (§43).

    Goes through the same ``store_upload`` path as a browser upload, so validation, magic-byte
    sniffing, size limits, checksums and de-duplication behave identically no matter how the
    bytes arrived.
    """
    session = get_session_or_404(db, session_id)
    device = _get_or_404(db, device_id)
    provider = get_provider(device.provider_key)
    config = device.config or {}

    try:
        discovered = list(provider.discover(config))
    except UnsupportedOperation as exc:
        raise ValidationError(str(exc)) from exc

    wanted = set(filenames) if filenames else None
    selected = [item for item in discovered if wanted is None or item.filename in wanted]
    if not selected:
        raise ValidationError(
            "No matching files were found on the device",
            available=[item.filename for item in discovered][:50],
        )

    stored: list[CaptureFileOut] = []
    for item in selected:
        with provider.open_file(config, item.filename) as stream:
            record = capture_service.store_upload(
                db, session, filename=item.filename, stream=stream
            )
        stored.append(CaptureFileOut.model_validate(record))

    device.last_seen_at = datetime.now(UTC)
    db.commit()
    return stored


def _get_or_404(db: DbSession, device_id: str) -> CameraDevice:
    device = db.get(CameraDevice, device_id)
    if device is None:
        raise NotFoundError(f"Camera device {device_id} not found")
    return device
