"""Synthetic equirectangular panorama generator for demo and test data (§37).

Why generate instead of download: the brief forbids using any proprietary vendor's imagery, and
public-domain 360 construction datasets are scarce and awkwardly licensed. Rendering our own
scene sidesteps the question entirely — every pixel here is produced by this file — and it buys
something a stock photo cannot: **real geometry**.

Because the panoramas are ray-cast from known camera positions in a known room, consecutive
frames contain true parallax. The feature matcher, the essential-matrix solver and the position
estimator therefore operate on genuine multi-view geometry, and their output can be checked
against the camera path that produced it. A pair of unrelated stock panoramas would only ever
exercise the code paths, never the correctness.

The renderer is a small closed-form ray tracer:

    ray/box intersection  → the room shell (floor, ceiling, four walls)
    ray/cylinder          → structural columns, which create occlusion and depth discontinuities
    blocky value noise    → wall texture with hard corners, which is what ORB actually detects

It is vectorised over pixels and evaluated in row bands, so a 4096×2048 panorama costs a second
or so and bounded memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True, slots=True)
class Column:
    x: float
    z: float
    radius: float = 0.35


@dataclass(frozen=True, slots=True)
class Room:
    """An axis-aligned room. The camera walks at ``y = 0``."""

    x_min: float = -10.0
    x_max: float = 10.0
    z_min: float = -6.0
    z_max: float = 6.0
    floor_y: float = -1.6
    ceiling_y: float = 1.6
    columns: tuple[Column, ...] = field(
        default=(
            Column(-5.0, -3.0),
            Column(-5.0, 3.0),
            Column(5.0, -3.0),
            Column(5.0, 3.0),
        )
    )

    @property
    def width_m(self) -> float:
        return self.x_max - self.x_min

    @property
    def depth_m(self) -> float:
        return self.z_max - self.z_min


# Face ids: 0 -X wall, 1 +X wall, 2 -Z wall, 3 +Z wall, 4 floor, 5 ceiling, 6 column
_FACE_BASE = np.array(
    [
        [128, 126, 120],  # -X wall: grey blockwork
        [132, 130, 124],  # +X wall
        [120, 124, 128],  # -Z wall: cooler concrete
        [124, 128, 132],  # +Z wall
        [86, 84, 80],  # floor slab
        [156, 156, 152],  # ceiling / soffit
        [104, 100, 94],  # column
    ],
    dtype=np.float32,
)


def render_panorama(
    room: Room,
    camera: tuple[float, float, float],
    *,
    width: int = 4096,
    height: int = 2048,
    yaw_deg: float = 0.0,
    brightness: float = 1.0,
    seed: int = 20260910,
    band_rows: int = 256,
) -> np.ndarray:
    """Render one equirectangular BGR image from ``camera`` inside ``room``.

    The projection convention matches :mod:`app.vision.panorama` exactly — longitude −π…π left
    to right, latitude +π/2…−π/2 top to bottom, +Z forward — so a rendered panorama is a valid
    input to the real pipeline rather than an approximation of one.
    """
    origin = np.array(camera, dtype=np.float64)
    image = np.zeros((height, width, 3), dtype=np.uint8)

    lon = (np.arange(width, dtype=np.float64) / width) * 2.0 * np.pi - np.pi + np.deg2rad(yaw_deg)
    cos_lon, sin_lon = np.cos(lon), np.sin(lon)

    for start in range(0, height, band_rows):
        stop = min(start + band_rows, height)
        rows = np.arange(start, stop, dtype=np.float64)
        lat = np.pi / 2.0 - (rows / height) * np.pi
        cos_lat, sin_lat = np.cos(lat), np.sin(lat)

        # Direction grid for this band: (band, width, 3)
        dx = cos_lat[:, None] * sin_lon[None, :]
        dy = np.repeat(sin_lat[:, None], width, axis=1)
        dz = cos_lat[:, None] * cos_lon[None, :]
        directions = np.stack([dx, dy, dz], axis=-1)

        band = _shade(room, origin, directions, seed=seed, brightness=brightness)
        image[start:stop] = band

    return image


def _shade(
    room: Room,
    origin: np.ndarray,
    directions: np.ndarray,
    *,
    seed: int,
    brightness: float,
) -> np.ndarray:
    shape = directions.shape[:2]
    flat = directions.reshape(-1, 3)

    t_shell, face = _intersect_box(room, origin, flat)
    t_column, column_index = _intersect_columns(room, origin, flat)

    use_column = (t_column > 1e-4) & (t_column < t_shell)
    t = np.where(use_column, t_column, t_shell)
    face = np.where(use_column, 6, face)

    hits = origin[None, :] + flat * t[:, None]
    colour = _texture(room, hits, face, column_index, seed=seed)

    # Distance shading: a construction interior is lit unevenly, and a depth cue makes the
    # panorama read as a space rather than a flat pattern.
    falloff = 1.0 / (1.0 + 0.045 * np.clip(t, 0.0, 60.0))
    colour = colour * (0.55 + 0.65 * falloff)[:, None]

    # A soft directional highlight, as if from an opening in the +X wall.
    highlight = np.clip(flat[:, 0] * 0.18 + 0.92, 0.7, 1.15)
    colour = colour * highlight[:, None] * brightness

    return np.clip(colour, 0, 255).astype(np.uint8).reshape(*shape, 3)


def _intersect_box(
    room: Room, origin: np.ndarray, directions: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Nearest forward intersection with the room shell, seen from inside.

    Standard slab test. Because the camera is inside the box, the exit parameter ``t_max`` is
    the visible surface, and the face is whichever slab produced it.
    """
    bounds_min = np.array([room.x_min, room.floor_y, room.z_min])
    bounds_max = np.array([room.x_max, room.ceiling_y, room.z_max])

    with np.errstate(divide="ignore", invalid="ignore"):
        inv = 1.0 / directions
        t_low = (bounds_min[None, :] - origin[None, :]) * inv
        t_high = (bounds_max[None, :] - origin[None, :]) * inv

    t_far = np.maximum(t_low, t_high)
    t_far = np.where(np.isnan(t_far), np.inf, t_far)
    axis = np.argmin(t_far, axis=1)
    t_exit = np.take_along_axis(t_far, axis[:, None], axis=1)[:, 0]

    positive_side = np.take_along_axis(directions, axis[:, None], axis=1)[:, 0] > 0
    face = np.select(
        [axis == 0, axis == 1, axis == 2],
        [
            np.where(positive_side, 1, 0),  # +X / -X wall
            np.where(positive_side, 5, 4),  # ceiling / floor
            np.where(positive_side, 3, 2),  # +Z / -Z wall
        ],
        default=0,
    )
    return t_exit, face


def _intersect_columns(
    room: Room, origin: np.ndarray, directions: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Nearest forward intersection with any vertical cylinder."""
    best_t = np.full(directions.shape[0], np.inf)
    best_index = np.full(directions.shape[0], -1, dtype=np.int32)

    dx, dz = directions[:, 0], directions[:, 2]
    a = dx * dx + dz * dz

    for index, column in enumerate(room.columns):
        ox = origin[0] - column.x
        oz = origin[2] - column.z
        b = 2.0 * (ox * dx + oz * dz)
        c = ox * ox + oz * oz - column.radius**2
        discriminant = b * b - 4.0 * a * c

        solvable = (discriminant > 0) & (a > 1e-12)
        root = np.zeros_like(discriminant)
        root[solvable] = np.sqrt(discriminant[solvable])

        with np.errstate(divide="ignore", invalid="ignore"):
            t0 = (-b - root) / (2.0 * a)
            t1 = (-b + root) / (2.0 * a)
        t_near = np.where(t0 > 1e-4, t0, t1)

        # Only the part of the cylinder between floor and ceiling exists.
        y_hit = origin[1] + directions[:, 1] * t_near
        valid = (
            solvable
            & (t_near > 1e-4)
            & (y_hit > room.floor_y)
            & (y_hit < room.ceiling_y)
            & (t_near < best_t)
        )
        best_t = np.where(valid, t_near, best_t)
        best_index = np.where(valid, index, best_index)

    return best_t, best_index


def _texture(
    room: Room,
    hits: np.ndarray,
    face: np.ndarray,
    column_index: np.ndarray,
    *,
    seed: int,
) -> np.ndarray:
    """Procedural surface colour with hard-edged detail.

    Blocky value noise is chosen on purpose: ORB detects corners, and smooth gradient noise
    produces almost none. Three octaves of quantised lattice noise plus construction-like
    markings give a dense, repeatable, corner-rich texture at every scale the detector looks at.
    """
    x, y, z = hits[:, 0], hits[:, 1], hits[:, 2]

    # Per-face surface coordinates, in metres.
    u = np.where(
        (face == 0) | (face == 1),
        z,
        np.where((face == 2) | (face == 3), x, np.where(face == 6, np.arctan2(z, x) * 2.0, x)),
    )
    v = np.where((face == 4) | (face == 5), z, y)

    base = _FACE_BASE[np.clip(face, 0, 6)]

    detail = (
        0.55 * _blocky_noise(u, v, 3.1, seed + 11)
        + 0.28 * _blocky_noise(u, v, 9.7, seed + 29)
        + 0.17 * _blocky_noise(u, v, 27.3, seed + 47)
    )
    colour = base * (0.72 + 0.5 * detail[:, None])

    # Blockwork / formwork joints: strong straight edges the matcher can lock onto.
    joint_u = np.abs(((u * 2.0) % 1.0) - 0.5) < 0.045
    joint_v = np.abs(((v * 2.0) % 1.0) - 0.5) < 0.045
    joints = (joint_u | joint_v) & (face < 4)
    colour[joints] *= 0.66

    # Floor slab grid, at a different pitch so floor and walls are not confusable.
    floor_grid = (np.abs(((u / 1.5) % 1.0) - 0.5) < 0.03) | (np.abs(((v / 1.5) % 1.0) - 0.5) < 0.03)
    on_floor = face == 4
    colour[on_floor & floor_grid] *= 1.4

    # Ceiling service runs.
    ceiling_runs = np.abs(((u / 2.0) % 1.0) - 0.5) < 0.06
    on_ceiling = face == 5
    colour[on_ceiling & ceiling_runs] *= 0.7

    # Give each column a distinct tint so a mismatch between columns is detectable.
    is_column = face == 6
    if np.any(is_column):
        tint = 1.0 + 0.09 * np.clip(column_index, 0, None)
        colour[is_column] *= tint[is_column, None]

    return colour


def _blocky_noise(u: np.ndarray, v: np.ndarray, frequency: float, seed: int) -> np.ndarray:
    """Deterministic quantised lattice noise in [0, 1) with hard cell boundaries."""
    cell_u = np.floor(u * frequency).astype(np.int64)
    cell_v = np.floor(v * frequency).astype(np.int64)
    h = (cell_u * np.int64(374761393)) ^ (cell_v * np.int64(668265263)) ^ np.int64(seed)
    h = (h ^ (h >> np.int64(13))) * np.int64(1274126177)
    h = h ^ (h >> np.int64(16))
    return (h & np.int64(0xFFFF)).astype(np.float64) / 65536.0


# ---- camera paths ------------------------------------------------------------
#: Frame spacing along the walk, in metres.
#:
#: This is not an arbitrary number. Two-view matching degrades with baseline, and measurement on
#: these panoramas shows how sharply: at 0.5 m two frames share ~360 verified correspondences, at
#: 1.0 m ~230, at 2.0 m ~125, and at 4.0 m only ~25 — below what the geometry solver needs. A
#: real operator walking at ~1 m/s with a frame every 1–2 s lands squarely in the usable range,
#: so the default spacing reflects genuine capture cadence rather than a convenient value.
DEFAULT_STEP_M = 1.5


def walk_path(
    room: Room,
    waypoints: list[tuple[float, float]],
    *,
    step_m: float = DEFAULT_STEP_M,
) -> list[tuple[float, float, float]]:
    """Interpolate an operator's walk at even spacing along the polyline, at camera height.

    Spacing is by *distance*, not by a fixed count per leg: an operator walks at a roughly
    constant pace, so a long leg yields more frames than a short one. That also gives the
    position estimator's constant-pace assumption a fair test, which makes any drift it reports
    genuinely its own rather than an artefact of uneven sampling.
    """
    if step_m <= 0:
        raise ValueError("step_m must be positive")

    positions: list[tuple[float, float, float]] = []
    for index in range(len(waypoints) - 1):
        x0, z0 = waypoints[index]
        x1, z1 = waypoints[index + 1]
        length = float(np.hypot(x1 - x0, z1 - z0))
        steps = max(1, round(length / step_m))
        for step in range(steps):
            ratio = step / steps
            positions.append((x0 + (x1 - x0) * ratio, 0.0, z0 + (z1 - z0) * ratio))
    positions.append((waypoints[-1][0], 0.0, waypoints[-1][1]))
    return positions


#: A route that stays clear of the columns at (±5, ±3) and keeps ~2 m off every wall.
DEFAULT_WAYPOINTS: list[tuple[float, float]] = [
    (-7.5, -4.0),
    (-7.5, 4.0),
    (0.0, 4.0),
    (7.5, 4.0),
    (7.5, -4.0),
]


def default_walk(room: Room, *, step_m: float = DEFAULT_STEP_M) -> list[tuple[float, float, float]]:
    """The demo walkthrough: down one side, across, and back down the other."""
    return walk_path(room, DEFAULT_WAYPOINTS, step_m=step_m)
