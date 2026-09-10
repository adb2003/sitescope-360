"""Generate a to-scale floor-plan raster for the demo project (§37).

Drawn from the same :class:`~app.processing.synthetic.Room` the panoramas are rendered from, so
the demo's floor plan and its imagery describe the same building. That consistency is what makes
the demo actually demonstrate something: a capture point dropped where the operator walked lines
up with the geometry visible in the panorama, and the pixel-to-metre calibration is exact rather
than invented.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from app.processing.synthetic import Room

#: Drawing scale. 40 px/m gives a 20 m room an 800 px wide plan — enough to place points
#: precisely without producing an unwieldy image.
PX_PER_METER = 40.0
MARGIN_PX = 60

PAPER = (250, 249, 246)
INK = (38, 44, 48)
WALL = (30, 36, 40)
GRID = (222, 226, 228)
COLUMN_FILL = (150, 156, 160)
ACCENT = (15, 90, 78)


def render_floor_plan(
    room: Room,
    *,
    title: str = "Level 01 — General Arrangement",
    project_code: str = "DEMO-001",
    px_per_meter: float = PX_PER_METER,
) -> tuple[bytes, int, int, float]:
    """Return ``(png_bytes, width_px, height_px, px_per_meter)``."""
    inner_w = int(room.width_m * px_per_meter)
    inner_h = int(room.depth_m * px_per_meter)
    width = inner_w + 2 * MARGIN_PX
    height = inner_h + 2 * MARGIN_PX + 40

    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)

    def to_px(x_m: float, z_m: float) -> tuple[float, float]:
        """World metres → plan pixels. +Z world runs *down* the page, as on a plan."""
        return (
            MARGIN_PX + (x_m - room.x_min) * px_per_meter,
            MARGIN_PX + (z_m - room.z_min) * px_per_meter,
        )

    # 1 m grid
    x_m = room.x_min
    while x_m <= room.x_max + 1e-6:
        x0, y0 = to_px(x_m, room.z_min)
        x1, y1 = to_px(x_m, room.z_max)
        draw.line([(x0, y0), (x1, y1)], fill=GRID, width=1)
        x_m += 1.0
    z_m = room.z_min
    while z_m <= room.z_max + 1e-6:
        x0, y0 = to_px(room.x_min, z_m)
        x1, y1 = to_px(room.x_max, z_m)
        draw.line([(x0, y0), (x1, y1)], fill=GRID, width=1)
        z_m += 1.0

    # Structural shell, drawn as a double line the way a wall is on a real drawing.
    top_left = to_px(room.x_min, room.z_min)
    bottom_right = to_px(room.x_max, room.z_max)
    draw.rectangle([top_left, bottom_right], outline=WALL, width=6)
    draw.rectangle(
        [
            (top_left[0] + 9, top_left[1] + 9),
            (bottom_right[0] - 9, bottom_right[1] - 9),
        ],
        outline=WALL,
        width=1,
    )

    # Columns
    for column in room.columns:
        cx, cy = to_px(column.x, column.z)
        r = column.radius * px_per_meter * 1.6
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=COLUMN_FILL, outline=WALL, width=2)

    # An opening in the +X wall, matching the directional highlight in the panoramas.
    door_a = to_px(room.x_max, -1.2)
    door_b = to_px(room.x_max, 1.2)
    draw.line([door_a, door_b], fill=PAPER, width=8)
    draw.arc(
        [
            door_a[0] - 2.4 * px_per_meter,
            door_a[1],
            door_a[0] + 2.4 * px_per_meter,
            door_a[1] + 4.8 * px_per_meter,
        ],
        start=180,
        end=270,
        fill=INK,
        width=2,
    )

    # Dimension strings
    _dimension_h(draw, to_px, room, px_per_meter)
    _dimension_v(draw, to_px, room, px_per_meter)

    # Title block
    draw.text((MARGIN_PX, 18), title, fill=INK)
    draw.text(
        (MARGIN_PX, 34), f"{project_code}   ·   scale {px_per_meter:.0f} px = 1 m", fill=ACCENT
    )
    draw.text(
        (MARGIN_PX, height - 26),
        "SiteScope 360 — generated demo drawing (not a real building)",
        fill=(120, 128, 132),
    )

    # North arrow
    nx, ny = width - MARGIN_PX + 8, MARGIN_PX + 10
    draw.line([(nx, ny + 28), (nx, ny)], fill=INK, width=2)
    draw.polygon([(nx, ny - 6), (nx - 5, ny + 3), (nx + 5, ny + 3)], fill=INK)
    draw.text((nx - 4, ny + 32), "N", fill=INK)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), width, height, px_per_meter


def _dimension_h(draw: ImageDraw.ImageDraw, to_px, room: Room, scale: float) -> None:
    y = to_px(0, room.z_max)[1] + 24
    x0 = to_px(room.x_min, 0)[0]
    x1 = to_px(room.x_max, 0)[0]
    draw.line([(x0, y), (x1, y)], fill=INK, width=1)
    for x in (x0, x1):
        draw.line([(x, y - 5), (x, y + 5)], fill=INK, width=1)
    draw.text(((x0 + x1) / 2 - 18, y + 6), f"{room.width_m:.1f} m", fill=INK)


def _dimension_v(draw: ImageDraw.ImageDraw, to_px, room: Room, scale: float) -> None:
    x = to_px(room.x_min, 0)[0] - 24
    y0 = to_px(0, room.z_min)[1]
    y1 = to_px(0, room.z_max)[1]
    draw.line([(x, y0), (x, y1)], fill=INK, width=1)
    for y in (y0, y1):
        draw.line([(x - 5, y), (x + 5, y)], fill=INK, width=1)
    draw.text((x - 18, (y0 + y1) / 2 - 6), f"{room.depth_m:.1f}", fill=INK)


def world_to_plan_px(
    room: Room, x_m: float, z_m: float, *, px_per_meter: float = PX_PER_METER
) -> tuple[float, float]:
    """The inverse mapping the seeder needs to place capture points where the camera stood."""
    return (
        MARGIN_PX + (x_m - room.x_min) * px_per_meter,
        MARGIN_PX + (z_m - room.z_min) * px_per_meter,
    )
