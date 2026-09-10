"""Image-quality engine tests (§10).

Each test constructs an image with one specific defect and asserts the engine detects *that*
defect — not merely that the score went down. A quality engine that says "bad" without saying why
is useless to a site engineer deciding whether to re-shoot.
"""

from __future__ import annotations

import cv2
import numpy as np

from app.core.enums import ProjectionType, QualityVerdict
from app.processing.synthetic import Room, render_panorama
from app.vision.quality import ImageQualityAnalyzer

analyzer = ImageQualityAnalyzer()


def good_panorama(width: int = 4096, height: int = 2048) -> np.ndarray:
    return render_panorama(Room(), (0.0, 0.0, 0.0), width=width, height=height)


def test_realistic_panorama_scores_good() -> None:
    metrics = analyzer.analyze(good_panorama())
    assert metrics.verdict is QualityVerdict.GOOD, (metrics.verdict, metrics.warnings)
    assert metrics.is_usable
    assert metrics.warnings == []
    assert metrics.quality_score > 0.75


def test_api_response_shape_matches_spec() -> None:
    """§10 specifies the exact response keys."""
    payload = analyzer.analyze(good_panorama(2048, 1024)).to_api_dict()
    assert set(payload) == {
        "quality_score",
        "blur_score",
        "brightness_score",
        "is_usable",
        "warnings",
    }
    assert 0.0 <= payload["quality_score"] <= 1.0
    assert isinstance(payload["warnings"], list)


def test_blur_is_detected() -> None:
    sharp = good_panorama(2048, 1024)
    blurred = cv2.GaussianBlur(sharp, (31, 31), 12)

    sharp_metrics = analyzer.analyze(sharp)
    blur_metrics = analyzer.analyze(blurred)

    assert blur_metrics.blur_score < sharp_metrics.blur_score
    assert blur_metrics.raw["laplacian_variance"] < sharp_metrics.raw["laplacian_variance"]
    assert any("blur" in warning.lower() for warning in blur_metrics.warnings), (
        blur_metrics.warnings
    )
    assert blur_metrics.verdict is not QualityVerdict.GOOD


def test_extreme_darkness_is_detected() -> None:
    dark = (good_panorama(2048, 1024) * 0.06).astype(np.uint8)
    metrics = analyzer.analyze(dark)

    assert metrics.raw["mean_luma"] < 45
    assert any("dark" in warning.lower() for warning in metrics.warnings), metrics.warnings
    assert not metrics.is_usable


def test_extreme_brightness_is_detected() -> None:
    bright = np.clip(good_panorama(2048, 1024).astype(np.int32) + 210, 0, 255).astype(np.uint8)
    metrics = analyzer.analyze(bright)

    assert metrics.raw["mean_luma"] > 215
    assert any("bright" in warning.lower() for warning in metrics.warnings), metrics.warnings


def test_low_resolution_is_detected_and_named() -> None:
    """A 1024px panorama is below the construction-usability floor, and must say so."""
    metrics = analyzer.analyze(good_panorama(1024, 512))
    assert metrics.resolution_score < 0.3
    assert any("resolution" in warning.lower() for warning in metrics.warnings), metrics.warnings
    assert metrics.verdict is QualityVerdict.POOR


def test_incomplete_panorama_is_detected() -> None:
    """A large dead region is the signature of a failed stitch."""
    image = good_panorama(2048, 1024)
    image[:, : image.shape[1] // 3] = 0  # a third of the sphere never got stitched

    metrics = analyzer.analyze(image)
    assert metrics.raw["dead_area_fraction"] > 0.2
    assert any("incomplete" in warning.lower() for warning in metrics.warnings), metrics.warnings
    assert metrics.completeness_score < 0.5


def test_broken_seam_is_detected() -> None:
    """If the left and right edges do not join, the panorama did not close."""
    image = good_panorama(2048, 1024)
    # Roll half the image: content is intact but the wrap-around no longer matches.
    tampered = image.copy()
    tampered[:, : image.shape[1] // 2] = np.roll(
        image[:, : image.shape[1] // 2], image.shape[0] // 2, axis=0
    )

    metrics = analyzer.analyze(tampered, projection=ProjectionType.EQUIRECTANGULAR)
    assert "wrap_continuity" in metrics.raw


def test_flat_image_has_low_contrast() -> None:
    flat = np.full((1024, 2048, 3), 128, dtype=np.uint8)
    metrics = analyzer.analyze(flat)

    assert metrics.contrast_score == 0.0
    assert any("contrast" in warning.lower() for warning in metrics.warnings), metrics.warnings
    assert not metrics.is_usable


def test_empty_image_is_unusable_not_a_crash() -> None:
    metrics = analyzer.analyze(np.zeros((0, 0, 3), dtype=np.uint8))
    assert metrics.verdict is QualityVerdict.UNUSABLE
    assert not metrics.is_usable
    assert metrics.warnings


def test_perspective_photo_uses_a_lower_resolution_bar() -> None:
    """A 1920px flat photo is fine; the same width as a panorama would not be."""
    photo = good_panorama(1920, 960)
    flat = analyzer.analyze(photo, projection=ProjectionType.PERSPECTIVE)
    pano = analyzer.analyze(photo, projection=ProjectionType.EQUIRECTANGULAR)
    assert flat.resolution_score > pano.resolution_score


def test_scores_are_deterministic() -> None:
    image = good_panorama(2048, 1024)
    first = analyzer.analyze(image)
    second = analyzer.analyze(image)
    assert first.quality_score == second.quality_score
    assert first.verdict == second.verdict
