"""Media decoding, metadata extraction, frame extraction, and derivative generation (§7, §9).

Deliberately small surface: the pipeline stages call these functions and never touch OpenCV or
Pillow directly, so swapping a decoder (PyAV, ffmpeg CLI) is a change in one file.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import cv2
import numpy as np
from PIL import ExifTags, Image, ImageFile

from app.core.config import settings
from app.core.enums import FileKind
from app.core.errors import ProcessingError, UnsupportedMediaError

# Site panoramas are large; Pillow's default bomb guard is too tight for an 11k×5.5k equirect.
Image.MAX_IMAGE_PIXELS = 300_000_000
# Some 360 cameras emit JPEGs with slightly truncated final scans. Refusing them loses real data.
ImageFile.LOAD_TRUNCATED_IMAGES = True

#: Magic-byte prefixes. Extensions are attacker-controlled; these are not (§44).
_MAGIC: tuple[tuple[bytes, str, FileKind], ...] = (
    (b"\xff\xd8\xff", "image/jpeg", FileKind.PANORAMA),
    (b"\x89PNG\r\n\x1a\n", "image/png", FileKind.PANORAMA),
)
_VIDEO_BRANDS = frozenset(
    {
        b"ftypmp4",
        b"ftypisom",
        b"ftypiso2",
        b"ftypmp41",
        b"ftypmp42",
        b"ftypavc1",
        b"ftypqt  ",
        b"ftypM4V ",
        b"ftypXAVC",
    }
)

_GPANO_PROJECTION = re.compile(rb"GPano:ProjectionType>?\s*[=>]?\s*[\"']?([A-Za-z]+)")
_GPANO_CROPPED_WIDTH = re.compile(rb"GPano:CroppedAreaImageWidthPixels[>=]\s*[\"']?(\d+)")
_GPANO_FULL_WIDTH = re.compile(rb"GPano:FullPanoWidthPixels[>=]\s*[\"']?(\d+)")


@dataclass(slots=True)
class SniffResult:
    content_type: str
    kind: FileKind
    is_video: bool


@dataclass(slots=True)
class MediaMetadata:
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    fps: float | None = None
    captured_at: datetime | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    gps_latitude: float | None = None
    gps_longitude: float | None = None
    gps_altitude_m: float | None = None
    xmp_projection: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# ---- sniffing ----------------------------------------------------------------
def sniff(header: bytes, filename: str) -> SniffResult:
    """Identify a file from its leading bytes, cross-checked against its extension."""
    extension = Path(filename).suffix.lower()

    for prefix, content_type, kind in _MAGIC:
        if header.startswith(prefix):
            if extension in settings.allowed_video_extensions:
                raise UnsupportedMediaError(f"{filename} has a video extension but image content")
            return SniffResult(content_type, kind, is_video=False)

    # ISO-BMFF (MP4/MOV): the brand box starts at offset 4.
    if len(header) >= 12 and any(header[4:12].startswith(brand) for brand in _VIDEO_BRANDS):
        content_type = "video/quicktime" if extension == ".mov" else "video/mp4"
        return SniffResult(content_type, FileKind.VIDEO_360, is_video=True)

    if header[:4] == b"%PDF":
        return SniffResult("application/pdf", FileKind.UNKNOWN, is_video=False)

    raise UnsupportedMediaError(
        f"{filename} is not a recognised JPEG, PNG, MP4 or MOV file",
        detected_prefix=header[:12].hex(),
    )


def checksum(stream: BinaryIO) -> str:
    """SHA-256, streaming so a 2 GiB video is never held in memory."""
    digest = hashlib.sha256()
    stream.seek(0)
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


# ---- image decoding ----------------------------------------------------------
def decode_image(data: bytes) -> np.ndarray:
    """Decode to a BGR ndarray. Raises :class:`ProcessingError` on corrupt input."""
    array = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise ProcessingError("Image could not be decoded — the file is corrupt or unsupported")
    return image


def load_image(path: str | Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ProcessingError(f"Image could not be decoded: {path}")
    return image


def encode_jpeg(image: np.ndarray, quality: int = 85) -> bytes:
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise ProcessingError("JPEG encoding failed")
    return bytes(buffer)


def resize_to_width(image: np.ndarray, target_width: int) -> np.ndarray:
    height, width = image.shape[:2]
    if width <= target_width:
        return image
    scale = target_width / width
    return cv2.resize(
        image,
        (target_width, max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


# ---- WebP --------------------------------------------------------------------
#: Every image this pipeline *generates* is WebP. Measured against the JPEG settings it replaced,
#: at matched PSNR on a rendered 4096x2048 panorama: thumbnails 41% smaller, previews 38%,
#: full frames 48%. A five-minute walk at one frame per second is ~300 frames, so the full-frame
#: saving alone is about 110 MB per walkthrough.
#:
#: Files the *operator* uploaded are never re-encoded. A still upload's frame deliberately points
#: at the source object (see ``_is_derived`` in the stages module), and converting it would mean
#: lossily re-encoding somebody's master to save space on a copy we did not have to make.
WEBP_EXTENSION = ".webp"
WEBP_CONTENT_TYPE = "image/webp"

#: Chosen per size to match or beat the PSNR of the JPEG quality each one replaced, rather than
#: to a round number: 78 -> 80 (38.8 dB -> 39.1), 82 -> 85 (42.1 -> 42.0), 90 -> 90 (45.3 -> 45.5).
WEBP_QUALITY_THUMB = 80
WEBP_QUALITY_PREVIEW = 85
WEBP_QUALITY_FULL = 90


def encode_webp(image: np.ndarray, quality: int = WEBP_QUALITY_FULL) -> bytes:
    """Encode BGR pixels as WebP.

    OpenCV first because it is about six times faster than Pillow on a full panorama, and that
    difference is multiplied by every frame of every walkthrough. Its WebP support depends on how
    the wheel was built, though, so Pillow -- which bundles libwebp and always has it -- stands
    behind it rather than letting a differently-built OpenCV turn frame extraction into a crash.
    """
    ok, buffer = cv2.imencode(WEBP_EXTENSION, image, [int(cv2.IMWRITE_WEBP_QUALITY), quality])
    if ok:
        return bytes(buffer)
    return _encode_webp_pillow(image, quality)


def _encode_webp_pillow(image: np.ndarray, quality: int) -> bytes:
    """Fallback encoder. Separate so a test can exercise it without breaking OpenCV."""
    try:
        # OpenCV arrays are BGR and Pillow expects RGB; getting this backwards produces a
        # perfectly valid file with the red and blue channels swapped, which no size or status
        # check would catch.
        buffer = io.BytesIO()
        Image.fromarray(image[:, :, ::-1]).save(buffer, format="WEBP", quality=quality, method=4)
        return buffer.getvalue()
    except Exception as exc:
        raise ProcessingError(f"WebP encoding failed: {exc}") from exc


def make_derivatives(image: np.ndarray) -> tuple[bytes, bytes]:
    """``(thumbnail_webp, preview_webp)`` — three sizes per frame keeps the UI cheap (§35)."""
    thumb = encode_webp(resize_to_width(image, settings.thumbnail_width), WEBP_QUALITY_THUMB)
    preview = encode_webp(resize_to_width(image, settings.preview_width), WEBP_QUALITY_PREVIEW)
    return thumb, preview


# ---- metadata ----------------------------------------------------------------
def extract_image_metadata(data: bytes) -> MediaMetadata:
    """EXIF + XMP GPano. Never raises: missing metadata is normal, not an error."""
    meta = MediaMetadata()
    try:
        with Image.open(io.BytesIO(data)) as pil:
            meta.width, meta.height = pil.size
            exif = pil.getexif()
            if exif:
                _read_exif(exif, meta)
    except Exception as exc:
        meta.raw["metadata_error"] = str(exc)

    projection, gpano = _read_gpano(data)
    if projection:
        meta.xmp_projection = projection
    if gpano:
        meta.raw["gpano"] = gpano
    return meta


def _read_exif(exif: Any, meta: MediaMetadata) -> None:
    tags = {ExifTags.TAGS.get(tag, str(tag)): value for tag, value in exif.items()}
    meta.camera_make = _clean(tags.get("Make"))
    meta.camera_model = _clean(tags.get("Model"))

    for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        parsed = _parse_exif_datetime(tags.get(key))
        if parsed:
            meta.captured_at = parsed
            break

    stored = {
        key: _jsonable(value)
        for key, value in tags.items()
        if key not in {"MakerNote", "UserComment", "PrintImageMatching"}
    }

    try:
        gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:
        gps_ifd = None
    if gps_ifd:
        gps = {ExifTags.GPSTAGS.get(tag, str(tag)): value for tag, value in gps_ifd.items()}
        meta.gps_latitude = _dms_to_degrees(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
        meta.gps_longitude = _dms_to_degrees(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
        altitude = gps.get("GPSAltitude")
        if altitude is not None:
            with contextlib.suppress(TypeError, ValueError):
                meta.gps_altitude_m = float(altitude)
        stored["GPS"] = {key: _jsonable(value) for key, value in gps.items()}

    meta.raw["exif"] = stored


def _read_gpano(data: bytes) -> tuple[str | None, dict[str, Any] | None]:
    """Scan for an XMP GPano block.

    Reading the raw bytes rather than a full XMP parse is intentional: the block may be split
    across APP1 segments in ways Pillow does not reassemble, and all we need is the projection
    type and the cropped/full width ratio that tells us whether the panorama is complete.
    """
    window = data[:262144]
    if b"GPano" not in window:
        return None, None

    projection = None
    match = _GPANO_PROJECTION.search(window)
    if match:
        projection = match.group(1).decode("ascii", "ignore")

    info: dict[str, Any] = {}
    cropped = _GPANO_CROPPED_WIDTH.search(window)
    full = _GPANO_FULL_WIDTH.search(window)
    if cropped and full:
        try:
            cropped_px, full_px = int(cropped.group(1)), int(full.group(1))
            info["cropped_area_image_width_px"] = cropped_px
            info["full_pano_width_px"] = full_px
            if full_px > 0:
                info["horizontal_coverage"] = round(cropped_px / full_px, 4)
        except ValueError:
            pass
    if projection:
        info["projection_type"] = projection
    return projection, info or None


def extract_video_metadata(path: str | Path) -> MediaMetadata:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise ProcessingError(
            "Video could not be opened. The container or codec is unsupported by this build "
            "of OpenCV."
        )
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_total = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    finally:
        capture.release()

    duration = frame_total / fps if fps > 0 and frame_total > 0 else None
    return MediaMetadata(
        width=width or None,
        height=height or None,
        fps=fps or None,
        duration_seconds=duration,
        raw={"frame_count": frame_total, "fps": fps},
    )


# ---- video frame extraction --------------------------------------------------
def iter_video_frames(
    path: str | Path,
    *,
    interval_seconds: float | None = None,
    max_frames: int | None = None,
) -> Iterator[tuple[float, np.ndarray]]:
    """Yield ``(timestamp_seconds, bgr_frame)`` every ``interval_seconds`` (§9).

    Frames are *skipped by decoding and discarding*, not by seeking. Seeking with
    ``CAP_PROP_POS_FRAMES`` on a long-GOP 360 video is both slow (it re-decodes from the previous
    keyframe anyway) and unreliable across containers; a sequential read with a modulo test is
    faster and exact.

    Never decodes every frame of the video into memory — one frame is live at a time.
    """
    # `is None`, not `or`: an explicit 0 is a caller error that must surface, and `0 or default`
    # would silently substitute the default instead of raising below.
    step_seconds = (
        settings.frame_interval_seconds if interval_seconds is None else float(interval_seconds)
    )
    if step_seconds <= 0:
        raise ProcessingError("frame_interval_seconds must be greater than zero")
    limit = settings.max_frames_per_video if max_frames is None else int(max_frames)
    if limit <= 0:
        raise ProcessingError("max_frames must be greater than zero")

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise ProcessingError("Video could not be opened for frame extraction")

    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if fps <= 0 or not np.isfinite(fps):
            # Unknown frame rate: fall back to a common capture rate and record the assumption.
            fps = 30.0
        stride = max(1, round(step_seconds * fps))

        index = 0
        emitted = 0
        while emitted < limit:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            if index % stride == 0:
                yield (index / fps, frame)
                emitted += 1
            index += 1
    finally:
        capture.release()


def planned_frame_count(duration_seconds: float | None, interval_seconds: float) -> int | None:
    if not duration_seconds or interval_seconds <= 0:
        return None
    return max(1, int(duration_seconds // interval_seconds) + 1)


# ---- small helpers -----------------------------------------------------------
def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip("\x00")
    return text or None


def _parse_exif_datetime(value: Any) -> datetime | None:
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _dms_to_degrees(dms: Any, ref: Any) -> float | None:
    if not dms:
        return None
    try:
        degrees, minutes, seconds = (float(part) for part in dms)
    except (TypeError, ValueError):
        return None
    value = degrees + minutes / 60.0 + seconds / 3600.0
    if _clean(ref) in {"S", "W"}:
        value = -value
    return round(value, 8) if -180.0 <= value <= 180.0 else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value[:64].hex()
    if isinstance(value, str | int | float | bool | type(None)):
        return value
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value[:32]]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in list(value.items())[:64]}
    return str(value)[:300]
