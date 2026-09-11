"""Ground-truth tests for the spherical two-view geometry.

A synthetic scene of 3D points is projected into two equirectangular views with a *known* camera
pose. The estimator must recover that pose from the projections alone. This is the only way to
know the geometry is right rather than merely plausible — a mapper can look convincing on real
imagery while being systematically wrong.
"""

from __future__ import annotations

from unittest import mock

import numpy as np
import pytest

from app.core.enums import ProjectionType
from app.vision import motion as motion_module
from app.vision.features import verify_spherical
from app.vision.motion import (
    MIN_AMBIGUITY_MARGIN,
    EssentialMotionEstimator,
    _decompose,
    _essential_from_bearings,
)
from app.vision.panorama import bearings_to_pixels, pixels_to_bearings
from app.vision.types import FeatureSet, MatchResult

WIDTH, HEIGHT = 4096, 2048


def rot_y(degrees: float) -> np.ndarray:
    theta = np.deg2rad(degrees)
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def scene_points(n: int = 300, seed: int = 7) -> np.ndarray:
    """Points scattered in a room-sized volume around the origin, none too close."""
    rng = np.random.default_rng(seed)
    points = rng.uniform(-12.0, 12.0, size=(n, 3))
    points[:, 1] = rng.uniform(-2.5, 2.5, size=n)  # a room, not a cube
    # Push points away from both camera centres so projections stay well conditioned.
    norms = np.linalg.norm(points, axis=1, keepdims=True)
    return points / np.maximum(norms, 1e-9) * np.maximum(norms, 3.0)


def project(points: np.ndarray, rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    """Project world points into an equirectangular image for camera ``x = R·X + t``."""
    camera_points = points @ rotation.T + translation
    return bearings_to_pixels(camera_points, WIDTH, HEIGHT)


def test_pixel_bearing_roundtrip() -> None:
    rng = np.random.default_rng(3)
    pixels = np.column_stack([rng.uniform(0, WIDTH, 500), rng.uniform(1, HEIGHT - 1, 500)])
    bearings = pixels_to_bearings(pixels, WIDTH, HEIGHT)

    assert np.allclose(np.linalg.norm(bearings, axis=1), 1.0, atol=1e-9)
    back = bearings_to_pixels(bearings, WIDTH, HEIGHT)
    # Longitude wraps, so compare modulo the image width.
    dx = np.abs(back[:, 0] - pixels[:, 0]) % WIDTH
    dx = np.minimum(dx, WIDTH - dx)
    assert np.max(dx) < 1e-6
    assert np.max(np.abs(back[:, 1] - pixels[:, 1])) < 1e-6


@pytest.mark.parametrize(
    ("yaw_deg", "direction"),
    [
        (0.0, np.array([1.0, 0.0, 0.0])),  # strafe right, no turn
        (0.0, np.array([0.0, 0.0, 1.0])),  # walk forward
        (25.0, np.array([0.0, 0.0, 1.0])),  # walk forward while turning
        (-40.0, np.array([0.7, 0.0, 0.7])),  # diagonal with a left turn
    ],
)
def test_recovers_known_pose(yaw_deg: float, direction: np.ndarray) -> None:
    points = scene_points()
    move = 1.5 * direction / np.linalg.norm(direction)

    rotation_1_to_2 = rot_y(yaw_deg)
    # Camera 1 at origin, identity. Camera 2 centred at ``move``, rotated by ``rotation_1_to_2``.
    translation = -rotation_1_to_2 @ move

    px1 = project(points, np.eye(3), np.zeros(3))
    px2 = project(points, rotation_1_to_2, translation)

    matches = MatchResult(
        points_a=px1, points_b=px2, raw_match_count=len(px1), inlier_count=len(px1)
    )
    motion = EssentialMotionEstimator().estimate(matches, WIDTH, HEIGHT)

    assert motion.confidence > 0.5, motion.reasons
    assert motion.yaw_deg == pytest.approx(yaw_deg, abs=1.0)

    expected = move / np.linalg.norm(move)
    cosine = float(np.dot(motion.translation, expected))
    assert cosine > 0.99, f"translation direction off: cos={cosine:.4f}"


def test_cheirality_choice_is_not_a_near_tie_on_a_clean_pair() -> None:
    """The four-fold decomposition must win *clearly*, not by a hair.

    `_decompose` picks among four (R, t) candidates by counting points in front of both cameras.
    When two candidates score almost equally the winner is chosen by numerical noise rather than
    by the data, and a wrongly picked `t` is roughly 180 degrees wrong -- dead reckoning then
    carries that error through every subsequent frame. So the margin over the runner-up is
    reported and feeds the confidence.

    This test pins the well-conditioned case: on exact projections the margin must be wide. It is
    the baseline that makes a *narrow* margin meaningful as a warning signal. Measured margins on
    the demo walkthrough's real ORB matches run 0.86-1.00, consistent with this.
    """
    points = scene_points(n=300, seed=11)
    rotation = rot_y(12.0)
    baseline = np.array([1.4, 0.0, 0.35])

    px_a = project(points, np.eye(3), np.zeros(3))
    px_b = project(points, rotation, -rotation @ baseline)

    b1 = pixels_to_bearings(px_a, WIDTH, HEIGHT)
    b2 = pixels_to_bearings(px_b, WIDTH, HEIGHT)
    essential = _essential_from_bearings(b1, b2)
    assert essential is not None

    _, _, cheirality_ratio, margin = _decompose(essential, b1, b2)

    assert cheirality_ratio > 0.9, f"cheirality ratio {cheirality_ratio:.3f}"
    assert margin > MIN_AMBIGUITY_MARGIN, f"margin {margin:.3f} is a near-tie on a clean pair"


def test_a_near_tied_decomposition_is_refused_not_published() -> None:
    """A near-tie must be refused outright, not published with a reduced score.

    This is the distinction the earlier version of this code got wrong. The four candidates differ
    by a sign on the translation, so a near-tie does not mean "somewhat less accurate" -- it means
    the direction of travel is undetermined, and picking wrong is roughly 180 degrees out. Scaling
    it as one term of a five-term geometric mean left a coin-flip pair at confidence 0.60, which
    is above `MAPPING_CONFIDENCE_THRESHOLD` (0.45): it would have been published as an automatic
    path. A fifth root cannot express a disqualifier.

    Verified by calling the estimator with a hand-built margin rather than by contriving imagery
    that ties, because the policy is what is under test -- driving it through the renderer would
    test the renderer's ability to produce a tie instead.
    """
    points = scene_points(n=300, seed=11)
    rotation = rot_y(12.0)
    baseline = np.array([1.4, 0.0, 0.35])
    px_a = project(points, np.eye(3), np.zeros(3))
    px_b = project(points, rotation, -rotation @ baseline)
    good_matches = MatchResult(px_a, px_b, len(px_a), len(px_a))

    estimator = EssentialMotionEstimator()
    clean = estimator.estimate(good_matches, WIDTH, HEIGHT)
    assert clean.confidence > 0.7, f"a clean pair should be confident, got {clean.confidence:.3f}"

    # Same imagery, but the decomposition reports a coin flip between two candidates.
    with mock.patch.object(
        motion_module, "_decompose", return_value=(rotation, baseline, 0.95, 0.02)
    ):
        tied = estimator.estimate(good_matches, WIDTH, HEIGHT)

    assert tied.confidence == 0.0, f"a near-tie must be refused, got {tied.confidence:.3f}"
    assert np.allclose(tied.translation, 0.0), "no translation may be published from a near-tie"
    assert any("near-tied" in reason for reason in tied.reasons), tied.reasons


def test_pure_rotation_is_refused() -> None:
    """Turning in place must not be reported as movement (§46)."""
    points = scene_points()
    rotation = rot_y(35.0)

    px1 = project(points, np.eye(3), np.zeros(3))
    px2 = project(points, rotation, np.zeros(3))

    matches = MatchResult(px1, px2, len(px1), len(px1))
    motion = EssentialMotionEstimator().estimate(matches, WIDTH, HEIGHT)

    assert motion.confidence == 0.0
    assert np.allclose(motion.translation, 0.0)
    assert any("rotation in place" in reason for reason in motion.reasons)


def test_ransac_rejects_outliers() -> None:
    """Half the correspondences are garbage; verification must find the good half."""
    points = scene_points(n=200, seed=11)
    move = np.array([0.0, 0.0, 2.0])
    rotation = rot_y(10.0)

    px1 = project(points, np.eye(3), np.zeros(3))
    px2 = project(points, rotation, -rotation @ move)

    rng = np.random.default_rng(5)
    corrupted = px2.copy()
    outlier_index = rng.choice(len(px2), size=len(px2) // 2, replace=False)
    corrupted[outlier_index] = np.column_stack(
        [
            rng.uniform(0, WIDTH, len(outlier_index)),
            rng.uniform(1, HEIGHT - 1, len(outlier_index)),
        ]
    )

    mask = verify_spherical(px1, corrupted, WIDTH, HEIGHT)
    inlier_index = set(np.flatnonzero(mask).tolist())
    outliers = set(outlier_index.tolist())

    # Nearly every accepted match should be a true correspondence.
    assert len(inlier_index) >= 70
    contamination = len(inlier_index & outliers) / max(len(inlier_index), 1)
    assert contamination < 0.05, f"{contamination:.2%} of inliers are outliers"


def test_insufficient_matches_reports_zero_confidence() -> None:
    empty = np.zeros((0, 2))
    motion = EssentialMotionEstimator().estimate(MatchResult(empty, empty, 0, 0), WIDTH, HEIGHT)
    assert motion.confidence == 0.0
    assert not motion.is_reliable


def test_featureset_count_on_empty() -> None:
    features = FeatureSet(
        keypoints=np.zeros((0, 2), dtype=np.float32),
        descriptors=np.zeros((0, 32), dtype=np.uint8),
        image_width=WIDTH,
        image_height=HEIGHT,
        projection=ProjectionType.EQUIRECTANGULAR,
    )
    assert features.count == 0
