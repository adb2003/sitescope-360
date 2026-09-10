"""Camera provider implementations (§6, §43)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import BinaryIO

from app.cameras.base import (
    BaseCameraProvider,
    DiscoveredFile,
    ProviderCapabilities,
    UnsupportedOperation,
    resolve_path,
)
from app.core.enums import CameraTransport, CaptureSource, ProjectionType
from app.core.logging import get_logger

logger = get_logger(__name__)

_MEDIA_EXTENSIONS = (".jpg", ".jpeg", ".png", ".mp4", ".mov")


class LocalUploadProvider(BaseCameraProvider):
    """The MVP path: the operator hands files to the app through the browser or mobile app.

    Implemented by definition — the upload endpoint *is* this provider. It exists as a registered
    provider so a capture session always names its source, and so the UI can list it alongside
    hardware adapters without a special case.
    """

    capabilities = ProviderCapabilities(
        key="local_upload",
        display_name="Local file upload",
        transport=CameraTransport.FILE_UPLOAD,
        implemented=True,
        description=(
            "Record on any 360 camera, copy the files to your phone or computer, then upload "
            "them. Works with every camera and needs no integration."
        ),
        supports_live_transfer=False,
        supports_remote_trigger=False,
        setup_hint="No setup required.",
    )

    def health(self, config: dict[str, object]) -> dict[str, object]:
        return {
            "provider": self.capabilities.key,
            "implemented": True,
            "reachable": True,
            "note": "Uploads are handled by POST /api/captures/{id}/upload.",
        }


class FileImportProvider(BaseCameraProvider):
    """Import from a folder — an SD card, a mounted camera, or a sync directory.

    The one security-relevant detail: the folder is read from the *device registration* stored by
    an administrator, never from a request parameter. A client cannot ask the server to walk an
    arbitrary path.
    """

    capabilities = ProviderCapabilities(
        key="folder_import",
        display_name="Folder / SD card import",
        transport=CameraTransport.FOLDER_IMPORT,
        implemented=True,
        description=(
            "Reads 360 images and videos from a folder on the server — an inserted SD card, a "
            "mounted camera, or a synced directory."
        ),
        supports_live_transfer=True,
        supports_remote_trigger=False,
        setup_hint="Register the device with a 'folder' path in its configuration.",
        config_schema={"folder": "Absolute path to the folder to import from"},
    )

    def discover(self, config: dict[str, object]) -> Iterator[DiscoveredFile]:
        folder = resolve_path(config, "folder")
        recursive = bool(config.get("recursive", True))
        iterator = folder.rglob("*") if recursive else folder.glob("*")
        for path in sorted(iterator):
            if not path.is_file():
                continue
            if path.suffix.lower() not in _MEDIA_EXTENSIONS:
                continue
            yield DiscoveredFile(
                filename=path.name,
                size_bytes=path.stat().st_size,
                open_stream=path.open,
                source_path=str(path),
            )

    def open_file(self, config: dict[str, object], identifier: str) -> BinaryIO:
        folder = resolve_path(config, "folder")
        # Resolve and confirm containment: an identifier is a filename, not a path to trust.
        candidate = (folder / PurePosixPath(identifier).name).resolve()
        if not candidate.is_relative_to(folder.resolve()):
            raise UnsupportedOperation("Requested file is outside the configured folder")
        if not candidate.is_file():
            raise UnsupportedOperation(f"File not found in the import folder: {identifier}")
        return candidate.open("rb")

    def health(self, config: dict[str, object]) -> dict[str, object]:
        try:
            folder = resolve_path(config, "folder")
        except UnsupportedOperation as exc:
            return {
                "provider": self.capabilities.key,
                "implemented": True,
                "reachable": False,
                "error": str(exc),
            }
        count = sum(
            1
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.lower() in _MEDIA_EXTENSIONS
        )
        return {
            "provider": self.capabilities.key,
            "implemented": True,
            "reachable": True,
            "folder": str(folder),
            "media_files_available": count,
        }


class Generic360Provider(BaseCameraProvider):
    """Open Spherical Camera (OSC) over Wi-Fi — partial.

    OSC is an open specification, so this adapter needs no vendor SDK. ``health`` and ``discover``
    speak the documented ``/osc/info`` and ``/osc/commands/execute`` endpoints. Real cameras vary
    in which optional commands they implement, so this is marked *partial*: it is honest about
    being unverified against physical hardware in this build rather than claiming support.
    """

    capabilities = ProviderCapabilities(
        key="osc",
        display_name="Generic 360 camera (OSC over Wi-Fi)",
        transport=CameraTransport.WIFI_OSC,
        implemented=False,
        description=(
            "Talks the open OSC (Open Spherical Camera) HTTP protocol. Untested against physical "
            "hardware in this build — use folder import or upload for production capture."
        ),
        supports_live_transfer=True,
        supports_remote_trigger=True,
        setup_hint=(
            "Join the camera's Wi-Fi, then register the device with "
            '{"host": "192.168.1.1", "port": 80}.'
        ),
        config_schema={"host": "Camera IP address", "port": "HTTP port (usually 80)"},
    )

    def _base_url(self, config: dict[str, object]) -> str:
        host = config.get("host")
        if not host or not isinstance(host, str):
            raise UnsupportedOperation("OSC provider config requires a 'host'")
        raw_port = config.get("port", 80)
        port = int(raw_port) if isinstance(raw_port, int | str) else 80
        return f"http://{host}:{port}"

    def health(self, config: dict[str, object]) -> dict[str, object]:
        import httpx

        try:
            url = self._base_url(config)
        except UnsupportedOperation as exc:
            return {
                "provider": self.capabilities.key,
                "implemented": False,
                "reachable": False,
                "error": str(exc),
            }
        try:
            response = httpx.get(f"{url}/osc/info", timeout=3.0)
            response.raise_for_status()
            info = response.json()
        except Exception as exc:
            return {
                "provider": self.capabilities.key,
                "implemented": False,
                "reachable": False,
                "error": f"Could not reach the camera at {url}: {exc}",
            }
        return {
            "provider": self.capabilities.key,
            "implemented": False,
            "reachable": True,
            "camera": {
                "manufacturer": info.get("manufacturer"),
                "model": info.get("model"),
                "firmware": info.get("firmwareVersion"),
            },
            "note": "OSC reachable. File transfer is not verified against hardware.",
        }

    def trigger_capture(self, config: dict[str, object]) -> dict[str, object]:
        raise UnsupportedOperation(
            "Remote triggering over OSC is not enabled in this build. It is a Phase 2 item — "
            "see docs/CAMERA_INTEGRATION.md."
        )


class Insta360Provider(BaseCameraProvider):
    """Vendor adapter — interface only, deliberately unimplemented.

    Insta360's transfer protocol needs the vendor's own SDK. Declaring the seam without
    implementing it keeps the platform honest: nothing here pretends to support hardware it has
    not been tested against, and the class shows exactly where that support would slot in.
    """

    capabilities = ProviderCapabilities(
        key="insta360",
        display_name="Insta360 (vendor adapter)",
        transport=CameraTransport.WIFI_OSC,
        implemented=False,
        description=(
            "Placeholder for a vendor-specific adapter. Not implemented — use folder import or "
            "upload. Implementing it requires the vendor SDK and its licence terms."
        ),
        setup_hint="Not available. Copy files from the camera and upload them instead.",
    )


class FutureCameraProvider(BaseCameraProvider):
    """Copy this class to add a camera. See docs/CAMERA_INTEGRATION.md for the checklist."""

    capabilities = ProviderCapabilities(
        key="template",
        display_name="Template provider",
        transport=CameraTransport.FILE_UPLOAD,
        implemented=False,
        description="A worked template for new camera adapters. Not selectable for capture.",
        default_capture_source=CaptureSource.ROBOT,
        default_projection=ProjectionType.EQUIRECTANGULAR,
    )


_PROVIDERS: dict[str, BaseCameraProvider] = {
    provider.capabilities.key: provider
    for provider in (
        LocalUploadProvider(),
        FileImportProvider(),
        Generic360Provider(),
        Insta360Provider(),
        FutureCameraProvider(),
    )
}


def get_provider(key: str) -> BaseCameraProvider:
    provider = _PROVIDERS.get(key)
    if provider is None:
        raise UnsupportedOperation(
            f"Unknown camera provider {key!r}. Available: {sorted(_PROVIDERS)}"
        )
    return provider


def list_providers() -> list[BaseCameraProvider]:
    return list(_PROVIDERS.values())


def selectable_providers() -> list[BaseCameraProvider]:
    """Providers a user may actually pick — the template is excluded."""
    return [p for p in _PROVIDERS.values() if p.capabilities.key != "template"]
