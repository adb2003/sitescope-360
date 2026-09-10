"""Equirectangular geometry helpers and panorama detection (§7).

The 2:1 aspect ratio is a *hint*, not a gate: some rigs emit 1.9:1 or add a metadata bar, and
partial panoramas are still worth viewing. So we score the evidence and let the caller decide,
rather than rejecting a valid panorama on arithmetic alone.
"""

from __future__ import annotations

import numpy as np

from app.core.enums import ProjectionType
from app.vision.types import ImageInfo

#: Below this width an equirectangular image cannot show useful detail on a construction site.
MIN_PANORAMA_WIDTH = 1024


def describe_image(image: np.ndarray, *, xmp_projection: str | None = None) -> ImageInfo:
    """Classify an image as equirectangular or perspective, with reasons.

    Evidence used, in decreasing weight:

    1. An XMP ``GPano:ProjectionType`` of ``equirectangular`` — authoritative when present.
    2. Aspect ratio near 2:1.
    3. Horizontal wrap continuity: in a full 360 panorama the left and right edges are
       neighbouring columns of the same scene, so they match far better than two random columns.
    4. Polar row uniformity: zenith and nadir rows compress to near-constant colour.
    """
    height, width = image.shape[:2]
    channels = 1 if image.ndim == 2 else image.shape[2]
    aspect = width / height if height else 0.0
    reasons: list[str] = []
    score = 0.0

    if xmp_projection and "equirect" in xmp_projection.lower():
        reasons.append("XMP GPano:ProjectionType=equirectangular")
        score += 0.6

    # Aspect ratio is graded on a continuum rather than with a single reject threshold, so a
    # 1.9:1 rig output is still recognised while a 4:3 photograph is clearly ruled out. Leaving
    # the middle band unscored — as a simple "reject below 1.2" test does — would classify an
    # ordinary 3:2 site photo as merely "ambiguous".
    aspect_error = abs(aspect - 2.0)
    if aspect_error <= 0.05:
        reasons.append(f"aspect ratio {aspect:.2f} ≈ 2:1")
        score += 0.30
    elif aspect_error <= 0.25:
        reasons.append(f"aspect ratio {aspect:.2f} near 2:1")
        score += 0.18
    elif aspect < 1.5:
        reasons.append(f"aspect ratio {aspect:.2f} is not panoramic")
        score -= 0.30
    elif aspect < 1.75:
        reasons.append(f"aspect ratio {aspect:.2f} is wide but short of 2:1")
        score -= 0.10

    wrap = wrap_continuity(image)
    if wrap is not None:
        if wrap >= 0.6:
            reasons.append(f"left/right edges are continuous (score {wrap:.2f})")
            score += 0.25
        elif wrap <= 0.25:
            reasons.append(f"left/right edges are discontinuous (score {wrap:.2f})")
            score -= 0.15

    polar = polar_uniformity(image)
    if polar is not None and polar >= 0.6:
        reasons.append(f"poles are compressed as expected (score {polar:.2f})")
        score += 0.15

    score = float(np.clip(score, 0.0, 1.0))
    projection = (
        ProjectionType.EQUIRECTANGULAR
        if score >= 0.4
        else ProjectionType.PERSPECTIVE
        if score <= 0.15
        else ProjectionType.UNKNOWN
    )

    return ImageInfo(
        width=width,
        height=height,
        channels=channels,
        projection=projection,
        aspect_ratio=aspect,
        equirect_confidence=score,
        reasons=reasons,
    )


#: How many interior offsets contribute to the continuity baseline.
_WRAP_BASELINE_SAMPLES = 24


def wrap_continuity(image: np.ndarray, band: int = 8) -> float | None:
    """1.0 = the left and right edges join seamlessly, 0.0 = they are unrelated.

    The seam difference is compared against the *distribution* of differences between columns the
    same distance apart elsewhere in the image, which makes the measure independent of resolution
    and of how detailed the scene is.

    The baseline is a **median over many offsets**, not a mean over a few. Detail is not spread
    evenly around a real room — a wall of blockwork and a blank soffit differ by an order of
    magnitude — so sampling three arbitrary positions and averaging them makes the reference
    depend on which happened to be picked. That misjudges a perfectly closed panorama whenever
    the seam falls in a busier region than the samples did.

    Known limitation: on content that is discontinuous *everywhere* (pure noise), the seam is
    genuinely typical of the image and this returns a high score. That is the measure behaving as
    defined rather than a defect, and it is why continuity is only one input to the quality
    verdict.
    """
    height, width = image.shape[:2]
    if width < 4 * band or height < 8:
        return None
    grey = _to_grey(image).astype(np.float32)

    # Across the seam, column ``W-band+i`` neighbours column ``i`` — the same ``band`` offset the
    # baseline below measures, so the two are directly comparable.
    seam_error = float(np.mean(np.abs(grey[:, -band:] - grey[:, :band])))

    offsets = np.linspace(0, width - 2 * band, _WRAP_BASELINE_SAMPLES, dtype=int)
    baselines = [
        float(
            np.mean(
                np.abs(grey[:, offset + band : offset + 2 * band] - grey[:, offset : offset + band])
            )
        )
        for offset in offsets
    ]
    if not baselines:
        return None
    baseline = max(float(np.median(baselines)), 1e-3)

    # seam_error ≈ baseline → continuous (score 1). seam_error ≫ baseline → broken (score 0).
    return float(np.clip(1.0 - (seam_error - baseline) / (4.0 * baseline), 0.0, 1.0))


def polar_uniformity(image: np.ndarray, rows: int = 4) -> float | None:
    """How constant the top and bottom rows are — a signature of equirectangular poles."""
    height, width = image.shape[:2]
    if height < 4 * rows or width < 16:
        return None
    grey = _to_grey(image).astype(np.float32)
    top_std = float(np.mean([np.std(grey[r]) for r in range(rows)]))
    bottom_std = float(np.mean([np.std(grey[height - 1 - r]) for r in range(rows)]))
    middle_std = float(np.std(grey[height // 2]))
    if middle_std < 1e-3:
        return None
    ratio = (top_std + bottom_std) / (2.0 * middle_std)
    return float(np.clip(1.0 - ratio, 0.0, 1.0))


def _to_grey(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    # Rec. 601 luma; cv2 delivers BGR.
    b, g, r = image[:, :, 0], image[:, :, 1], image[:, :, 2]
    return (0.114 * b + 0.587 * g + 0.299 * r).astype(np.uint8)


# ---- spherical geometry ------------------------------------------------------
def pixels_to_bearings(points: np.ndarray, width: int, height: int) -> np.ndarray:
    """Map equirectangular pixel coordinates to unit direction vectors.

    Convention: x spans longitude −π…π left to right, y spans latitude +π/2…−π/2 top to bottom.
    The returned frame is right-handed with +Z forward (image centre), +X right, +Y up.
    """
    if points.size == 0:
        return np.zeros((0, 3), dtype=np.float64)
    px = np.asarray(points, dtype=np.float64)
    lon = (px[:, 0] / width) * 2.0 * np.pi - np.pi
    lat = np.pi / 2.0 - (px[:, 1] / height) * np.pi
    cos_lat = np.cos(lat)
    return np.stack([cos_lat * np.sin(lon), np.sin(lat), cos_lat * np.cos(lon)], axis=1)


def bearings_to_pixels(bearings: np.ndarray, width: int, height: int) -> np.ndarray:
    """Inverse of :func:`pixels_to_bearings`."""
    if bearings.size == 0:
        return np.zeros((0, 2), dtype=np.float64)
    unit = bearings / np.maximum(np.linalg.norm(bearings, axis=1, keepdims=True), 1e-12)
    lon = np.arctan2(unit[:, 0], unit[:, 2])
    lat = np.arcsin(np.clip(unit[:, 1], -1.0, 1.0))
    x = (lon + np.pi) / (2.0 * np.pi) * width
    y = (np.pi / 2.0 - lat) / np.pi * height
    return np.stack([x, y], axis=1)


def equator_band_mask(width: int, height: int, keep_fraction: float = 0.5) -> np.ndarray:
    """A mask selecting the equatorial band of an equirectangular image.

    Features near the poles are stretched by ``1/cos(latitude)``, so their descriptors are
    unstable and their bearing estimates noisy. Restricting detection to the band around the
    horizon — which on a construction site is where the walls, columns and openings are —
    measurably improves match quality.
    """
    mask = np.zeros((height, width), dtype=np.uint8)
    keep = float(np.clip(keep_fraction, 0.1, 1.0))
    half = int(height * keep / 2)
    centre = height // 2
    mask[max(0, centre - half) : min(height, centre + half), :] = 255
    return mask


def dhash(image: np.ndarray, size: int = 8) -> str:
    """64-bit difference hash as 16 hex characters, for near-duplicate detection (§10).

    dHash compares adjacent pixel brightness rather than absolute values, so it is stable under
    exposure changes — which matters when the same corridor is shot twice minutes apart.
    """
    import cv2

    grey = _to_grey(image)
    resized = cv2.resize(grey, (size + 1, size), interpolation=cv2.INTER_AREA)
    diff = resized[:, 1:] > resized[:, :-1]
    bits = diff.flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:0{size * size // 4}x}"


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Bit distance between two hex hashes; 64 if they are not comparable."""
    if not hash_a or not hash_b or len(hash_a) != len(hash_b):
        return 64
    try:
        return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")
    except ValueError:
        return 64
