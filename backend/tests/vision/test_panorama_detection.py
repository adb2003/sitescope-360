"""Panorama detection and perceptual hashing (§7, §10)."""

from __future__ import annotations

import numpy as np

from app.core.enums import ProjectionType
from app.processing.synthetic import Room, render_panorama
from app.vision.panorama import (
    describe_image,
    dhash,
    hamming_distance,
    polar_uniformity,
    wrap_continuity,
)


def test_rendered_panorama_is_detected_as_equirectangular() -> None:
    image = render_panorama(Room(), (0.0, 0.0, 0.0), width=2048, height=1024)
    info = describe_image(image)

    assert info.projection is ProjectionType.EQUIRECTANGULAR
    assert info.aspect_ratio == 2.0
    assert info.equirect_confidence >= 0.4
    assert any("aspect ratio" in reason for reason in info.reasons)
    assert any("continuous" in reason for reason in info.reasons)


def test_xmp_metadata_is_authoritative() -> None:
    """A GPano tag should carry an image the geometry alone would call ambiguous."""
    # A 3:2 crop — not 2:1, so geometry is unconvincing on its own.
    image = render_panorama(Room(), (0.0, 0.0, 0.0), width=1500, height=1000)

    without = describe_image(image)
    with_xmp = describe_image(image, xmp_projection="equirectangular")

    assert with_xmp.equirect_confidence > without.equirect_confidence
    assert with_xmp.projection is ProjectionType.EQUIRECTANGULAR
    assert any("GPano" in reason for reason in with_xmp.reasons)


def test_narrow_photo_is_not_a_panorama() -> None:
    rng = np.random.default_rng(3)
    photo = rng.integers(0, 255, size=(1200, 1600, 3), dtype=np.uint8)
    info = describe_image(photo)

    assert info.projection is ProjectionType.PERSPECTIVE
    assert info.equirect_confidence < 0.4
    assert any("not panoramic" in reason for reason in info.reasons)


def test_unusual_but_valid_panorama_is_not_rejected() -> None:
    """§7: do not reject a valid panorama on the aspect ratio alone."""
    # 1.9:1 — some rigs emit this, and it is still a usable 360 image.
    image = render_panorama(Room(), (0.0, 0.0, 0.0), width=1900, height=1000)
    info = describe_image(image)

    assert info.projection is not ProjectionType.PERSPECTIVE
    assert info.equirect_confidence > 0.15


def test_wrap_continuity_distinguishes_closed_from_broken() -> None:
    """A closed panorama scores high; the same content with a displaced seam scores low.

    The comparison is against the *same image* with only its wrap broken, because that isolates
    continuity from every other property. Comparing a panorama against unrelated noise would not:
    noise is discontinuous everywhere, so its seam is genuinely typical of the image and scores
    high — a documented limitation of the measure, not a failure to detect anything.
    """
    image = render_panorama(Room(), (0.0, 0.0, 0.0), width=1024, height=512)
    closed = wrap_continuity(image)
    assert closed is not None and closed > 0.5

    # Shift the right-hand half vertically: content is intact, but column W-1 no longer
    # neighbours column 0 — exactly what a failed stitch looks like.
    broken_image = image.copy()
    half = image.shape[1] // 2
    broken_image[:, half:] = np.roll(image[:, half:], image.shape[0] // 3, axis=0)

    broken = wrap_continuity(broken_image)
    assert broken is not None
    assert broken < closed, f"broken={broken:.2f} closed={closed:.2f}"


def test_wrap_continuity_is_resolution_independent() -> None:
    """The same closed panorama must score comparably at every resolution.

    A baseline sampled from only a handful of offsets makes this fail: at high resolution
    neighbouring columns are more similar, so the reference shrinks and a busy seam starts to
    look broken.
    """
    room = Room()
    scores = [
        wrap_continuity(render_panorama(room, (0.0, 0.0, 0.0), width=w, height=w // 2))
        for w in (1024, 2048, 4096)
    ]
    assert all(score is not None and score > 0.5 for score in scores), scores
    assert max(scores) - min(scores) < 0.35, scores  # type: ignore[type-var]


def test_polar_uniformity_is_higher_for_a_real_panorama() -> None:
    image = render_panorama(Room(), (0.0, 0.0, 0.0), width=1024, height=512)
    rng = np.random.default_rng(5)
    noise = rng.integers(0, 255, size=(512, 1024, 3), dtype=np.uint8)

    assert (polar_uniformity(image) or 0) > (polar_uniformity(noise) or 0)


def test_dhash_is_stable_under_exposure_change() -> None:
    """dHash compares adjacent pixels, so it survives a brightness shift — which is the point:
    the same corridor shot twice minutes apart must be recognised as a duplicate."""
    image = render_panorama(Room(), (0.0, 0.0, 0.0), width=1024, height=512)
    brighter = np.clip(image.astype(np.int32) + 28, 0, 255).astype(np.uint8)

    assert hamming_distance(dhash(image), dhash(brighter)) <= 6


def test_dhash_separates_different_viewpoints() -> None:
    room = Room()
    here = dhash(render_panorama(room, (-7.0, 0.0, -4.0), width=1024, height=512))
    there = dhash(render_panorama(room, (7.0, 0.0, 4.0), width=1024, height=512))

    assert hamming_distance(here, there) > 6


def test_dhash_format_and_bad_input() -> None:
    image = render_panorama(Room(), (0.0, 0.0, 0.0), width=512, height=256)
    value = dhash(image)
    assert len(value) == 16
    int(value, 16)  # must be valid hex

    assert hamming_distance("", value) == 64
    assert hamming_distance("zz", "zz") == 64
    assert hamming_distance(value, value) == 0
