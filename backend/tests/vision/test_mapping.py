"""Relative-positioning accuracy and — just as important — its refusal to guess (§14, §46).

Two properties are asserted:

1. **When it works, it is accurate.** Rendered from a known camera path, the estimated track
   aligns to ground truth to well under a metre after the similarity fit that anchoring performs.
2. **When it cannot work, it says so.** With frames spaced too far apart for two-view geometry,
   the mapper must report low confidence and demand manual placement rather than emit a path.

The second property is the one that matters commercially. A mapper that is right most of the time
and confidently wrong occasionally is worse than no mapper, because a site team will trust it.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.enums import MappingConfidence
from app.processing.synthetic import Room, default_walk, render_panorama, walk_path
from app.vision.engine import get_vision_engine
from app.vision.mapbuilder import MapBuilder
from app.vision.position import KeyframeSelector
from app.vision.types import RelativePose

WIDTH, HEIGHT = 1600, 800


@pytest.fixture(scope="module")
def engine():
    return get_vision_engine()


def _features(engine, positions):
    return [
        engine.detect_features(render_panorama(Room(), position, width=WIDTH, height=HEIGHT))
        for position in positions
    ]


@pytest.mark.slow
def test_track_matches_ground_truth_within_a_metre(engine) -> None:
    room = Room()
    positions = default_walk(room, step_m=1.5)
    features = _features(engine, positions)

    outcome = engine.estimate_track(features, [1.5] * (len(features) - 1))

    assert outcome.confidence in {MappingConfidence.MEDIUM, MappingConfidence.HIGH}
    assert not outcome.requires_manual_placement
    assert len(outcome.poses) == len(positions)

    # The estimator is scale-free by construction, so accuracy is only meaningful after the
    # similarity fit that anchoring to a floor plan performs. That is exactly how the platform
    # uses it, so it is what should be measured.
    truth = np.array([[p[0], p[2]] for p in positions])
    estimated = np.array([[pose.x, pose.y] for pose in outcome.poses])
    transform = MapBuilder().fit_transform(estimated, truth)
    assert transform is not None

    error = np.linalg.norm(transform.apply(estimated) - truth, axis=1)
    assert error.mean() < 1.0, f"mean error {error.mean():.2f} m"
    assert error.max() < 2.0, f"max error {error.max():.2f} m"
    # Recovered scale should be close to the assumed constant pace.
    assert 0.5 < transform.scale < 2.0


@pytest.mark.slow
def test_wide_spacing_is_refused_rather_than_guessed(engine) -> None:
    """Frames 6 m apart do not share enough geometry — the mapper must decline."""
    room = Room()
    positions = walk_path(room, [(-7.5, -4.0), (-7.5, 4.0), (7.5, 4.0)], step_m=6.0)
    features = _features(engine, positions)

    outcome = engine.estimate_track(features, [6.0] * (len(features) - 1))

    assert outcome.requires_manual_placement
    assert outcome.confidence in {MappingConfidence.NONE, MappingConfidence.LOW}
    assert "manual placement" in outcome.note.lower()
    # The note must quantify the problem, not just assert it.
    assert "%" in outcome.note


def test_single_frame_cannot_be_mapped(engine) -> None:
    outcome = engine.estimate_track([], [])
    assert outcome.confidence is MappingConfidence.NONE
    assert outcome.requires_manual_placement
    assert "at least two frames" in outcome.note


def test_anchoring_requires_two_points() -> None:
    builder = MapBuilder()
    poses = [
        RelativePose(0, 0.0, 0.0, 0.0, 1.0, 1.0),
        RelativePose(1, 1.0, 0.0, 0.0, 1.0, 1.0),
        RelativePose(2, 2.0, 0.0, 0.0, 1.0, 1.0),
    ]

    one_anchor = builder.place_track(poses, [(0, 100.0, 100.0)])
    assert not one_anchor.ok
    assert "at least 2" in one_anchor.note

    two_anchors = builder.place_track(poses, [(0, 100.0, 100.0), (2, 300.0, 100.0)])
    assert two_anchors.ok
    assert two_anchors.transform is not None
    # Track spans 2 units; anchors span 200px → scale 100 px/unit.
    assert two_anchors.transform.scale == pytest.approx(100.0, rel=1e-6)
    assert two_anchors.placed[1]["x"] == pytest.approx(200.0, abs=1e-6)


def test_collinear_anchors_are_refused() -> None:
    """Two anchors on top of each other leave scale undetermined."""
    builder = MapBuilder()
    poses = [
        RelativePose(0, 0.0, 0.0, 0.0, 1.0, 1.0),
        RelativePose(1, 0.0, 0.0, 0.0, 1.0, 1.0),
    ]
    result = builder.place_track(poses, [(0, 10.0, 10.0), (1, 90.0, 10.0)])
    assert not result.ok
    assert "collinear or coincident" in result.note


def test_similarity_fit_forbids_reflection() -> None:
    """A mirrored walkthrough is never the right answer, so reflection must be excluded."""
    builder = MapBuilder()
    source = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    mirrored = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, -1.0]])

    transform = builder.fit_transform(source, mirrored)
    assert transform is not None
    # A reflection-free fit cannot match a mirrored target exactly, so residual must be non-zero.
    assert transform.rms_error_px > 0.1


def test_keyframe_selector_drops_duplicates_and_bad_frames() -> None:
    selector = KeyframeSelector(duplicate_hamming_threshold=6, min_quality_score=0.35)
    hashes = [
        "ffffffffffffffff",
        "ffffffffffffffff",  # identical → duplicate
        "0000000000000000",  # far away → keep
        "0000000000000000",  # duplicate again
        "0f0f0f0f0f0f0f0f",  # keep
    ]
    scores = [0.9, 0.9, 0.9, 0.9, 0.1]  # last frame is too poor to use

    keep = selector.select(hashes, scores)
    assert keep == [True, False, True, False, False]


def test_keyframe_selector_always_keeps_something() -> None:
    """Discarding every frame is worse than useless."""
    selector = KeyframeSelector(min_quality_score=0.9)
    keep = selector.select(["a" * 16, "a" * 16], [0.1, 0.1])
    assert keep[0] is True


@pytest.mark.slow
def test_similarity_between_two_views_of_the_same_place_is_high(engine) -> None:
    room = Room()
    near_a = engine.detect_features(
        render_panorama(room, (0.0, 0.0, 0.0), width=WIDTH, height=HEIGHT)
    )
    near_b = engine.detect_features(
        render_panorama(room, (0.4, 0.0, 0.0), width=WIDTH, height=HEIGHT)
    )
    far = engine.detect_features(render_panorama(room, (8.0, 0.0, 5.0), width=WIDTH, height=HEIGHT))

    close_similarity = engine.similarity(near_a, near_b)
    far_similarity = engine.similarity(near_a, far)
    assert close_similarity > far_similarity
    assert 0.0 <= far_similarity <= close_similarity <= 1.0
