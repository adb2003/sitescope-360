"""Dual-fisheye to equirectangular stitching (§7, §9).

Consumer 360 cameras have two back-to-back lenses, each covering rather more than a hemisphere,
and many of them record the raw pair rather than a stitched panorama: two circles side by side in
one frame, black in the corners. Everything downstream of ingestion in this platform assumes
equirectangular input — the feature detector, the motion estimator, the viewer — so a dual-fisheye
file is not merely lower quality here, it is meaningless. Its pixels do not mean what the
equirectangular code reads them as.

The mapping is a resampling, computed once as a pair of lookup tables and then applied per frame:

1. For each output pixel, take the direction it represents on the sphere (the convention is
   `panorama.pixels_to_bearings`, and must stay that way).
2. Rotate that direction into each lens's own frame.
3. Keep the lenses whose field of view actually contains it, and project through the equidistant
   fisheye model ``r = R·θ/θ_max`` to a source pixel.
4. Where both lenses see it — the overlap band that back-to-back lenses are built to have —
   cross-fade, so the seam is a gradient rather than a line.

What this does **not** do is calibrate. Real cameras have lens centres a few pixels off, radii
that differ between lenses, a little roll, and a field of view that is not exactly what the
datasheet says. Those are per-model constants that have to be measured against that model, and
guessing them produces a seam that is visibly doubled or torn. So the geometry defaults are
derived from the image itself and every one of them is overridable — and the stitch is reported
as `estimated`, never as a faithful reconstruction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

#: Typical for back-to-back consumer 360 cameras: a little over a hemisphere each, so the two
#: images overlap in a band around the seam. Under 180° per lens there would be a gap no amount
#: of blending could fill.
DEFAULT_FOV_DEG = 190.0

#: Width of the cross-fade, in degrees of angle from the lens axis. Narrow enough that each lens
#: supplies its own good centre, wide enough that a small calibration error smears rather than
#: steps.
BLEND_BAND_DEG = 8.0

#: A dual-fisheye frame is two circles in a square-ish pair, so side by side it is about 2:1 —
#: the same ratio as equirectangular, which is exactly why the two cannot be told apart by shape.
_SIDE_BY_SIDE_RATIO = 2.0
_RATIO_TOLERANCE = 0.12

#: Corner darkness that says "these are circles on black" rather than a rectangular photograph.
#: An equirectangular panorama of an interior has content in its corners; a fisheye pair does not.
_DARK_CORNER_MAX = 40.0
#: How much brighter the lens centres must be than the corners before the shape is believable.
_CENTRE_CORNER_RATIO = 2.0


@dataclass(frozen=True, slots=True)
class FisheyeLens:
    """One circular fisheye image inside the source frame.

    ``rotation`` maps a world direction into this lens's frame, where the lens looks along +Z.
    """

    centre_x: float
    centre_y: float
    radius: float
    fov_deg: float = DEFAULT_FOV_DEG
    rotation: np.ndarray = field(default_factory=lambda: np.eye(3))

    @property
    def max_theta(self) -> float:
        """Half the field of view, in radians — the largest angle from the axis it can see."""
        return math.radians(self.fov_deg) / 2.0


@dataclass(frozen=True, slots=True)
class DualFisheyeLayout:
    """Where the two lenses sit in the frame, and which way they point."""

    front: FisheyeLens
    back: FisheyeLens
    #: How the defaults were arrived at, so a caller can say whether they were measured or assumed.
    source: str = "assumed from frame geometry"


def _rotation_y(degrees: float) -> np.ndarray:
    """Rotation about the vertical axis. The back lens is the front one turned around."""
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    return np.array([[cos, 0.0, sin], [0.0, 1.0, 0.0], [-sin, 0.0, cos]], dtype=np.float64)


def default_layout(
    width: int, height: int, *, fov_deg: float = DEFAULT_FOV_DEG
) -> DualFisheyeLayout:
    """Assume the usual side-by-side arrangement: left circle forward, right circle behind.

    Each lens gets half the frame, centred in its half, with the largest radius that fits. This
    is right for the common case and approximately right for most of the rest; it is not
    calibration, and the name says so.
    """
    half = width / 2.0
    radius = min(half, height) / 2.0
    return DualFisheyeLayout(
        front=FisheyeLens(half / 2.0, height / 2.0, radius, fov_deg, np.eye(3)),
        back=FisheyeLens(half + half / 2.0, height / 2.0, radius, fov_deg, _rotation_y(180.0)),
    )


def looks_like_dual_fisheye(image: np.ndarray) -> bool:
    """Is this two fisheye circles rather than an equirectangular panorama?

    Both are roughly 2:1, so the aspect ratio alone decides nothing — the test is the corners. A
    fisheye circle inscribed in its half leaves four dark corners; an equirectangular panorama of
    a room has ceiling and floor there. Being wrong in either direction is bad in a specific way:
    stitching a panorama that was already equirectangular destroys it, and *not* stitching a real
    fisheye pair feeds the mapper an image whose pixels do not mean what it thinks. So this is
    deliberately conservative and requires both signals to agree.
    """
    if image.ndim < 2:
        return False
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        return False
    if abs(width / height - _SIDE_BY_SIDE_RATIO) > _RATIO_TOLERANCE:
        return False

    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    box = max(4, min(height, width) // 12)

    corners = [
        grey[:box, :box],
        grey[:box, -box:],
        grey[-box:, :box],
        grey[-box:, -box:],
        # The join between the two circles is dark too, and is the signal that distinguishes a
        # side-by-side pair from one very wide circular image.
        grey[:box, width // 2 - box // 2 : width // 2 + box // 2],
        grey[-box:, width // 2 - box // 2 : width // 2 + box // 2],
    ]
    corner_mean = float(np.mean([float(np.mean(patch)) for patch in corners]))
    if corner_mean > _DARK_CORNER_MAX:
        return False

    centres = [
        grey[
            height // 2 - box : height // 2 + box,
            width // 4 - box : width // 4 + box,
        ],
        grey[
            height // 2 - box : height // 2 + box,
            3 * width // 4 - box : 3 * width // 4 + box,
        ],
    ]
    centre_mean = float(np.mean([float(np.mean(patch)) for patch in centres]))
    return centre_mean > max(corner_mean * _CENTRE_CORNER_RATIO, _DARK_CORNER_MAX)


def _lens_maps(
    lens: FisheyeLens, directions: np.ndarray, shape: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(map_x, map_y, weight)`` for one lens over every output pixel.

    ``weight`` is zero where this lens cannot see the direction, one well inside its field of
    view, and ramps between the two across :data:`BLEND_BAND_DEG`.
    """
    local = directions @ lens.rotation.T
    # Clipped because a direction is only unit-length to floating-point precision, and arccos of
    # 1.0000000001 is a NaN that would propagate silently into the map.
    theta = np.arccos(np.clip(local[:, 2], -1.0, 1.0))
    phi = np.arctan2(local[:, 1], local[:, 0])

    radius = lens.radius * theta / lens.max_theta
    map_x = (lens.centre_x + radius * np.cos(phi)).astype(np.float32)
    # Image rows increase downward while +Y is up, so the sine is subtracted rather than added.
    # Getting this backwards mirrors each lens vertically -- a picture that looks plausible until
    # you notice the ceiling is on the floor.
    map_y = (lens.centre_y - radius * np.sin(phi)).astype(np.float32)

    band = math.radians(BLEND_BAND_DEG)
    weight = np.clip((lens.max_theta - theta) / band, 0.0, 1.0)

    return (
        map_x.reshape(shape),
        map_y.reshape(shape),
        weight.reshape(shape).astype(np.float32),
    )


def stitch_to_equirectangular(
    image: np.ndarray,
    layout: DualFisheyeLayout | None = None,
    *,
    width: int | None = None,
    height: int | None = None,
) -> np.ndarray:
    """Resample a dual-fisheye frame into an equirectangular panorama.

    The output defaults to the same width as the input, which keeps roughly the source's angular
    resolution: a side-by-side pair is about 2:1 already, and each lens covers half the sphere.
    """
    if image.ndim != 3:
        raise ValueError("Dual-fisheye stitching needs a colour image")
    source_h, source_w = image.shape[:2]
    out_w = int(width or source_w)
    out_h = int(height or out_w // 2)
    if out_w < 2 or out_h < 1:
        raise ValueError("Output size must be at least 2x1")

    layout = layout or default_layout(source_w, source_h)

    # Pixel centres, so the sampled grid is symmetric about the image rather than half a pixel
    # off -- which at the poles is a visible smear.
    us, vs = np.meshgrid(np.arange(out_w) + 0.5, np.arange(out_h) + 0.5)
    lon = (us / out_w) * 2.0 * np.pi - np.pi
    lat = np.pi / 2.0 - (vs / out_h) * np.pi
    cos_lat = np.cos(lat)
    directions = np.stack(
        [cos_lat * np.sin(lon), np.sin(lat), cos_lat * np.cos(lon)], axis=-1
    ).reshape(-1, 3)

    shape = (out_h, out_w)
    front_x, front_y, front_w = _lens_maps(layout.front, directions, shape)
    back_x, back_y, back_w = _lens_maps(layout.back, directions, shape)

    # BORDER_REPLICATE rather than a constant: a direction just outside a lens's circle should
    # take the nearest real pixel, not black. Its weight is near zero there anyway, so this only
    # decides what the blend fades *towards*.
    front = cv2.remap(image, front_x, front_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    back = cv2.remap(image, back_x, back_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    total = front_w + back_w
    # Every direction on the sphere is inside at least one lens when the fields of view exceed
    # 180°, so this only guards against a caller supplying a layout that leaves a gap.
    safe = np.maximum(total, 1e-6)
    alpha = (front_w / safe)[..., None]

    blended = front.astype(np.float32) * alpha + back.astype(np.float32) * (1.0 - alpha)
    return np.clip(blended, 0, 255).astype(np.uint8)
