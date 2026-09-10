"""Camera provider registry (§6, §43, §47)."""

from app.cameras.base import (
    BaseCameraProvider,
    CameraProvider,
    DiscoveredFile,
    ProviderCapabilities,
    UnsupportedOperation,
)
from app.cameras.providers import (
    FileImportProvider,
    FutureCameraProvider,
    Generic360Provider,
    Insta360Provider,
    LocalUploadProvider,
    get_provider,
    list_providers,
    selectable_providers,
)

__all__ = [
    "BaseCameraProvider",
    "CameraProvider",
    "DiscoveredFile",
    "FileImportProvider",
    "FutureCameraProvider",
    "Generic360Provider",
    "Insta360Provider",
    "LocalUploadProvider",
    "ProviderCapabilities",
    "UnsupportedOperation",
    "get_provider",
    "list_providers",
    "selectable_providers",
]
