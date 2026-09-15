"""Dual-fisheye to equirectangular stitching (§7).

A stitch that is wrong does not look broken. Flip a sign and the ceiling swaps with the floor;
rotate the convention and the panorama is a perfectly good picture of the room facing the wrong
way. Both would sail through any check on size, format or "is it not black", and both would then
be fed to a feature matcher that has no way to notice.

So the anchor of this file is a comparison against ground truth the stitcher had no hand in: the
same room, same camera, rendered straight to equirectangular by `render_panorama`. If the stitch
agrees with that, its geometry is right; if it merely agrees with its own inverse, it proves
nothing, which is why the fisheye source is ray-traced from the room rather than re-projected
from a panorama.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.processing.synthetic import Room, render_dual_fisheye, render_panorama
from app.vision.fisheye import (
    DEFAULT_FOV_DEG,
    default_layout,
    looks_like_dual_fisheye,
    stitch_to_equirectangular,
)

ROOM = Room()
CAMERA = (0.0, 0.0, 0.0)


def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    """Correlation of two images' luminance, ignoring overall brightness and contrast."""
    x = a.astype(np.float64).mean(axis=2).ravel()
    y = b.astype(np.float64).mean(axis=2).ravel()
    x -= x.mean()
    y -= y.mean()
    denominator = float(np.linalg.norm(x) * np.linalg.norm(y))
    return 0.0 if denominator < 1e-9 else float(np.dot(x, y) / denominator)


@pytest.mark.slow
def test_a_stitch_matches_the_same_view_rendered_directly() -> None:
    """The load-bearing test: agree with a renderer that knows nothing about fisheye.

    Not pixel-exact, and could not be. The fisheye source has a fixed number of pixels spread
    over the sphere by an equidistant law, so resampling it to equirectangular loses detail near
    the poles and at the seam. What must survive is the *structure* — the walls, the columns and
    the floor in the right places, facing the right way.
    """
    fisheye = render_dual_fisheye(ROOM, CAMERA, width=2048, height=1024)
    truth = render_panorama(ROOM, CAMERA, width=1024, height=512)

    stitched = stitch_to_equirectangular(fisheye, width=1024, height=512)

    assert stitched.shape == truth.shape
    assert _correlation(stitched, truth) > 0.9, _correlation(stitched, truth)


@pytest.mark.slow
def test_the_stitch_is_not_flipped_or_turned_around() -> None:
    """Every wrong orientation this could have, checked against the one right one.

    A vertical flip, a horizontal flip and a half-turn in longitude are the three mistakes the
    geometry invites, and each produces an image that looks entirely reasonable on its own. Each
    must correlate *worse* with the truth than the unaltered stitch does.
    """
    fisheye = render_dual_fisheye(ROOM, CAMERA, width=2048, height=1024)
    truth = render_panorama(ROOM, CAMERA, width=1024, height=512)
    stitched = stitch_to_equirectangular(fisheye, width=1024, height=512)

    upright = _correlation(stitched, truth)
    assert upright > _correlation(stitched[::-1], truth), "a vertical flip fits as well"
    assert upright > _correlation(stitched[:, ::-1], truth), "a horizontal flip fits as well"
    assert upright > _correlation(np.roll(stitched, 512, axis=1), truth), "180° yaw fits as well"


@pytest.mark.slow
def test_the_seam_is_a_gradient_rather_than_an_edge() -> None:
    """The two lenses meet at longitude ±90°, and that meeting must not be a visible line.

    Measured as the brightest column-to-column step: if blending failed, the seam columns would
    jump far more than the room's own vertical edges do.
    """
    fisheye = render_dual_fisheye(ROOM, CAMERA, width=2048, height=1024)
    stitched = stitch_to_equirectangular(fisheye, width=1024, height=512).astype(np.float64)

    columns = stitched.mean(axis=(0, 2))
    steps = np.abs(np.diff(columns))
    # The seams sit a quarter and three quarters of the way across.
    seam_steps = np.concatenate([steps[250:262], steps[762:774]])

    assert seam_steps.max() < steps.max(), "the largest step in the image is at a seam"


def test_the_output_is_a_two_to_one_panorama_by_default() -> None:
    fisheye = render_dual_fisheye(ROOM, CAMERA, width=512, height=256)

    stitched = stitch_to_equirectangular(fisheye)

    assert stitched.shape[1] == stitched.shape[0] * 2


def test_the_default_layout_puts_the_lenses_where_the_circles_are() -> None:
    layout = default_layout(2048, 1024)

    assert (layout.front.centre_x, layout.front.centre_y) == (512.0, 512.0)
    assert (layout.back.centre_x, layout.back.centre_y) == (1536.0, 512.0)
    assert layout.front.radius == layout.back.radius == 512.0
    assert layout.front.fov_deg == DEFAULT_FOV_DEG
    # The back lens looks the other way; identity rotation would stitch the front view twice.
    assert not np.allclose(layout.back.rotation, np.eye(3))


# ---- detection ---------------------------------------------------------------------------
@pytest.mark.slow
def test_a_real_fisheye_pair_is_recognised() -> None:
    assert looks_like_dual_fisheye(render_dual_fisheye(ROOM, CAMERA, width=1024, height=512))


@pytest.mark.slow
def test_an_equirectangular_panorama_is_not_mistaken_for_one() -> None:
    """The dangerous direction: stitching an already-equirectangular panorama destroys it.

    Both formats are 2:1, so shape decides nothing; the corners are what separate them.
    """
    assert not looks_like_dual_fisheye(render_panorama(ROOM, CAMERA, width=1024, height=512))


def test_an_all_black_frame_is_not_a_fisheye_pair() -> None:
    """Dark corners alone are not evidence — a black frame has nothing but dark corners."""
    assert not looks_like_dual_fisheye(np.zeros((512, 1024, 3), dtype=np.uint8))


def test_a_frame_of_the_wrong_shape_is_rejected_without_inspecting_it() -> None:
    # A 4:3 photograph is not a side-by-side pair whatever its corners look like.
    assert not looks_like_dual_fisheye(np.zeros((768, 1024, 3), dtype=np.uint8))


def test_stitching_refuses_a_greyscale_frame() -> None:
    with pytest.raises(ValueError, match="colour"):
        stitch_to_equirectangular(np.zeros((256, 512), dtype=np.uint8))
