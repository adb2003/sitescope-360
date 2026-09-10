"""Camera abstraction (§6, §43, §47).

```
CameraProvider
    ├── LocalUploadProvider    implemented
    ├── FileImportProvider     implemented
    ├── Generic360Provider     partial  (Open Spherical Camera over HTTP)
    ├── Insta360Provider       interface only
    └── FutureCameraProvider   template
```

Two rules keep the platform from becoming hostage to one vendor's SDK:

1. The MVP path — *camera → files → app* — is served by ``LocalUploadProvider``, which needs no
   camera integration whatsoever. Every other provider is optional.
2. A provider declares its capabilities (``supports_live_transfer``, ``supports_remote_trigger``)
   rather than raising when an unsupported operation is attempted, so the UI can hide what a
   device cannot do instead of offering a button that fails.

Adding a camera means adding one class and registering it. No migration, no changes to the
pipeline, the API, or the frontend. See docs/CAMERA_INTEGRATION.md.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

from app.core.enums import CameraTransport, CaptureSource, ProjectionType


@dataclass(slots=True)
class DiscoveredFile:
    """A file a provider has found and can hand to the platform."""

    filename: str
    size_bytes: int
    open_stream: object
    source_path: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    key: str
    display_name: str
    transport: CameraTransport
    implemented: bool
    description: str
    supports_live_transfer: bool = False
    supports_remote_trigger: bool = False
    accepted_extensions: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".mp4", ".mov")
    default_projection: ProjectionType = ProjectionType.EQUIRECTANGULAR
    default_capture_source: CaptureSource = CaptureSource.HUMAN_OPERATOR
    setup_hint: str | None = None
    config_schema: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class CameraProvider(Protocol):
    """Every camera adapter implements this."""

    capabilities: ProviderCapabilities

    def discover(self, config: dict[str, object]) -> Iterator[DiscoveredFile]:
        """Yield files available for import from this device."""
        ...

    def open_file(self, config: dict[str, object], identifier: str) -> BinaryIO:
        """Open one discovered file for reading."""
        ...

    def trigger_capture(self, config: dict[str, object]) -> dict[str, object]:
        """Start a capture on the device, when the transport supports it."""
        ...

    def health(self, config: dict[str, object]) -> dict[str, object]:
        """Whether the device is reachable and configured correctly."""
        ...


class UnsupportedOperation(RuntimeError):
    """Raised when a provider is asked to do something its transport cannot do."""


class BaseCameraProvider:
    """Sensible defaults so an adapter only implements what it actually supports."""

    capabilities: ProviderCapabilities

    def discover(self, config: dict[str, object]) -> Iterator[DiscoveredFile]:
        raise UnsupportedOperation(
            f"{self.capabilities.display_name} does not support file discovery"
        )

    def open_file(self, config: dict[str, object], identifier: str) -> BinaryIO:
        raise UnsupportedOperation(
            f"{self.capabilities.display_name} does not support direct file reads"
        )

    def trigger_capture(self, config: dict[str, object]) -> dict[str, object]:
        raise UnsupportedOperation(
            f"{self.capabilities.display_name} does not support remote triggering"
        )

    def health(self, config: dict[str, object]) -> dict[str, object]:
        return {
            "provider": self.capabilities.key,
            "implemented": self.capabilities.implemented,
            "reachable": None,
            "note": "This provider reports no live connection state.",
        }


def resolve_path(config: dict[str, object], key: str = "folder") -> Path:
    """Read a filesystem path out of a provider config, refusing anything unusable."""
    raw = config.get(key)
    if not raw or not isinstance(raw, str):
        raise UnsupportedOperation(f"Provider config is missing a {key!r} path")
    path = Path(raw).expanduser()
    if not path.exists():
        raise UnsupportedOperation(f"Path does not exist: {path}")
    if not path.is_dir():
        raise UnsupportedOperation(f"Path is not a folder: {path}")
    return path
