"""Image quality engine (§10).

These are **classical measurements**, not a learned model:

* blur          — variance of the Laplacian
* brightness    — mean luma plus shadow/highlight clipping fractions
* contrast      — luma standard deviation
* resolution    — pixel width against a construction-usability floor
* completeness  — dead (pure black) area and, for panoramas, wrap-seam continuity

No claim is made that this matches any commercial vendor's proprietary quality algorithm; it
catches the failures that actually waste a site visit — a dark frame, a walking-blur frame, a
half-stitched panorama, a duplicate.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from app.core.enums import ProjectionType, QualityVerdict
from app.vision.panorama import wrap_continuity
from app.vision.types import QualityMetrics

# ---- calibration constants ---------------------------------------------------
# Laplacian variance: below FLOOR is unusable motion blur, above CEILING is definitely sharp.
BLUR_VARIANCE_FLOOR = 15.0
BLUR_VARIANCE_CEILING = 400.0

# Mean luma window that needs no warning.
BRIGHTNESS_IDEAL_LOW = 85.0
BRIGHTNESS_IDEAL_HIGH = 175.0
BRIGHTNESS_DARK_LIMIT = 45.0
BRIGHTNESS_BRIGHT_LIMIT = 215.0

CONTRAST_FLOOR = 12.0
CONTRAST_TARGET = 55.0

# Equirectangular width thresholds. 4096 px ≈ 11 px/degree, enough to read a rebar tag.
RESOLUTION_PANORAMA_GOOD = 4096
RESOLUTION_PANORAMA_MIN = 1024
RESOLUTION_FLAT_GOOD = 1920
RESOLUTION_FLAT_MIN = 640

#: Resolution score below which the frame is flagged as too small to be useful.
RESOLUTION_WARN_SCORE = 0.35

DEAD_AREA_WARN = 0.06
DEAD_AREA_FAIL = 0.20

USABLE_SCORE_FLOOR = 0.35

WEIGHTS = {
    "blur": 0.32,
    "brightness": 0.20,
    "contrast": 0.13,
    "resolution": 0.15,
    "completeness": 0.20,
}


class ImageQualityAnalyzer:
    """Scores one decoded image. Stateless and thread-safe."""

    def analyze(
        self,
        image: np.ndarray,
        *,
        projection: ProjectionType = ProjectionType.EQUIRECTANGULAR,
    ) -> QualityMetrics:
        if image is None or image.size == 0:
            return self._unusable("Image could not be decoded (corrupt or empty file)")

        height, width = image.shape[:2]
        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        # Score on a bounded working size: quality is a property of the scene, and a 12 MP
        # Laplacian on every frame of a long video is wasted time.
        work = _downscale(grey, 1600)

        warnings: list[str] = []
        raw: dict[str, Any] = {"width": width, "height": height}

        blur_score = self._blur(work, raw, warnings)
        brightness_score = self._brightness(work, raw, warnings)
        contrast_score = self._contrast(work, raw, warnings)
        resolution_score = self._resolution(width, projection, raw, warnings)
        completeness_score = self._completeness(image, work, projection, raw, warnings)

        quality_score = float(
            WEIGHTS["blur"] * blur_score
            + WEIGHTS["brightness"] * brightness_score
            + WEIGHTS["contrast"] * contrast_score
            + WEIGHTS["resolution"] * resolution_score
            + WEIGHTS["completeness"] * completeness_score
        )

        verdict = self._verdict(
            quality_score,
            blur_score=blur_score,
            brightness_score=brightness_score,
            completeness_score=completeness_score,
            resolution_score=resolution_score,
        )
        is_usable = verdict is not QualityVerdict.UNUSABLE and quality_score >= USABLE_SCORE_FLOOR

        return QualityMetrics(
            quality_score=round(quality_score, 4),
            blur_score=round(blur_score, 4),
            brightness_score=round(brightness_score, 4),
            contrast_score=round(contrast_score, 4),
            resolution_score=round(resolution_score, 4),
            completeness_score=round(completeness_score, 4),
            verdict=verdict,
            is_usable=is_usable,
            warnings=warnings,
            raw=raw,
        )

    # ---- individual measures -------------------------------------------
    def _blur(self, grey: np.ndarray, raw: dict[str, Any], warnings: list[str]) -> float:
        variance = float(cv2.Laplacian(grey, cv2.CV_64F).var())
        raw["laplacian_variance"] = round(variance, 2)
        score = _log_normalize(variance, BLUR_VARIANCE_FLOOR, BLUR_VARIANCE_CEILING)
        if variance < BLUR_VARIANCE_FLOOR:
            warnings.append("Severe blur — likely captured while walking or out of focus")
        elif score < 0.4:
            warnings.append("Noticeable blur")
        return score

    def _brightness(self, grey: np.ndarray, raw: dict[str, Any], warnings: list[str]) -> float:
        mean = float(np.mean(grey))
        dark_fraction = float(np.mean(grey < 8))
        bright_fraction = float(np.mean(grey > 247))
        raw.update(
            mean_luma=round(mean, 2),
            clipped_shadow_fraction=round(dark_fraction, 4),
            clipped_highlight_fraction=round(bright_fraction, 4),
        )

        if mean < BRIGHTNESS_DARK_LIMIT:
            warnings.append("Extremely dark — the scene is probably unreadable")
        elif mean > BRIGHTNESS_BRIGHT_LIMIT:
            warnings.append("Extremely bright — highlights are blown out")

        if mean < BRIGHTNESS_IDEAL_LOW:
            score = _linear(mean, 0.0, BRIGHTNESS_IDEAL_LOW)
        elif mean > BRIGHTNESS_IDEAL_HIGH:
            score = _linear(255.0 - mean, 0.0, 255.0 - BRIGHTNESS_IDEAL_HIGH)
        else:
            score = 1.0

        clipping_penalty = min(0.45, 1.6 * (dark_fraction + bright_fraction))
        if clipping_penalty > 0.2:
            warnings.append("Large clipped regions (pure black or pure white)")
        return float(np.clip(score - clipping_penalty, 0.0, 1.0))

    def _contrast(self, grey: np.ndarray, raw: dict[str, Any], warnings: list[str]) -> float:
        std = float(np.std(grey))
        raw["luma_std"] = round(std, 2)
        score = _linear(std, CONTRAST_FLOOR, CONTRAST_TARGET)
        if std < CONTRAST_FLOOR:
            warnings.append("Very low contrast — flat, featureless image")
        return score

    def _resolution(
        self,
        width: int,
        projection: ProjectionType,
        raw: dict[str, Any],
        warnings: list[str],
    ) -> float:
        if projection is ProjectionType.EQUIRECTANGULAR:
            minimum, good = RESOLUTION_PANORAMA_MIN, RESOLUTION_PANORAMA_GOOD
        else:
            minimum, good = RESOLUTION_FLAT_MIN, RESOLUTION_FLAT_GOOD
        raw["resolution_floor"] = minimum
        raw["resolution_target"] = good

        score = _linear(float(width), float(minimum) * 0.5, float(good))
        # The warning is driven by the score, not by a separate boundary test. Comparing width
        # against the floor with `<` left an image exactly at the floor scoring 0.14 with nothing
        # said about it — a verdict of "poor" with no reason is not actionable.
        if score < RESOLUTION_WARN_SCORE:
            warnings.append(
                f"Low resolution — {width}px wide; {good}px is recommended for site detail "
                f"(minimum useful width {minimum}px)"
            )
        return score

    def _completeness(
        self,
        image: np.ndarray,
        grey: np.ndarray,
        projection: ProjectionType,
        raw: dict[str, Any],
        warnings: list[str],
    ) -> float:
        dead_fraction = float(np.mean(grey < 4))
        raw["dead_area_fraction"] = round(dead_fraction, 4)
        score = 1.0
        if dead_fraction >= DEAD_AREA_FAIL:
            warnings.append(f"Incomplete panorama — {dead_fraction:.0%} of the frame is empty")
            score -= 0.7
        elif dead_fraction >= DEAD_AREA_WARN:
            warnings.append(f"{dead_fraction:.0%} of the frame is empty")
            score -= 0.3

        if projection is ProjectionType.EQUIRECTANGULAR:
            wrap = wrap_continuity(image)
            if wrap is not None:
                raw["wrap_continuity"] = round(wrap, 3)
                if wrap < 0.3:
                    warnings.append("Panorama seam does not close — stitching may have failed")
                    score -= 0.35
                elif wrap < 0.55:
                    score -= 0.1
        return float(np.clip(score, 0.0, 1.0))

    # ---- verdict --------------------------------------------------------
    def _verdict(
        self,
        quality_score: float,
        *,
        blur_score: float,
        brightness_score: float,
        completeness_score: float,
        resolution_score: float,
    ) -> QualityVerdict:
        # Any single catastrophic axis makes the frame unusable regardless of the average —
        # a perfectly exposed, perfectly sharp image of nothing is still not a capture.
        if min(blur_score, brightness_score, completeness_score) <= 0.08:
            return QualityVerdict.UNUSABLE
        if quality_score < USABLE_SCORE_FLOOR:
            return QualityVerdict.UNUSABLE
        if quality_score < 0.55 or min(blur_score, completeness_score, resolution_score) < 0.3:
            return QualityVerdict.POOR
        if quality_score < 0.75 or min(blur_score, brightness_score) < 0.55:
            return QualityVerdict.WARNING
        return QualityVerdict.GOOD

    def _unusable(self, reason: str) -> QualityMetrics:
        return QualityMetrics(
            quality_score=0.0,
            blur_score=0.0,
            brightness_score=0.0,
            contrast_score=0.0,
            resolution_score=0.0,
            completeness_score=0.0,
            verdict=QualityVerdict.UNUSABLE,
            is_usable=False,
            warnings=[reason],
            raw={},
        )


# ---- helpers -----------------------------------------------------------------
def _linear(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return float(np.clip((value - low) / (high - low), 0.0, 1.0))


def _log_normalize(value: float, low: float, high: float) -> float:
    """Blur perception is roughly logarithmic in Laplacian variance."""
    if value <= 0:
        return 0.0
    lo, hi = np.log10(max(low, 1e-6)), np.log10(max(high, low + 1e-6))
    return float(np.clip((np.log10(value) - lo) / (hi - lo), 0.0, 1.0))


def _downscale(grey: np.ndarray, max_width: int) -> np.ndarray:
    height, width = grey.shape[:2]
    if width <= max_width:
        return grey
    scale = max_width / width
    return cv2.resize(grey, (max_width, max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
