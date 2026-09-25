"""Recognising a place across captures, and refusing to.

The claim this code makes is narrow and easy to overstate: *this frame was taken from the spot
that frame was taken from*. Everything downstream — a whole capture's coordinates, and the
date-to-date comparison that depends on two frames sharing a location — rests on it being true,
and nothing downstream can tell when it is not. So most of these tests are refusals.

The one that matters most is `test_a_frame_taken_further_down_the_corridor_is_not_a_position`.
Two frames twenty metres apart in the same corridor share an enormous number of features and are
a near-perfect appearance match; the only thing separating them from a genuine revisit is
parallax. If that test passes for the wrong reason, every other guarantee here is decoration.

Free of ORB by design, like `test_slam.py`: correspondences are synthesised from known geometry,
so the thresholds mean the same thing on every platform. Real imagery goes through the whole
feature detector in `tests/integration/test_cross_session.py`.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.enums import ProjectionType
from app.vision.panorama import bearings_to_pixels
from app.vision.relocalize import (
    MAX_PARALLAX_DEG,
    MIN_ANCHOR_SEPARATION_PX,
    MIN_MATCH_INLIERS,
    MIN_TRANSFORM_MATCHES,
    PlaceMatch,
    ReferenceFrame,
    Relocalizer,
    anchors_are_usable,
)
from app.vision.types import FeatureSet, MatchResult

WIDTH, HEIGHT = 2048, 1024
#: Comfortably above `MIN_MATCH_INLIERS`, so a test that fails does so for its own reason.
POINTS = 140


def _bearings(count: int = POINTS, seed: int = 7) -> np.ndarray:
    """Unit vectors spread around the sphere, concentrated near the equator like ORB's band."""
    rng = np.random.default_rng(seed)
    azimuth = rng.uniform(-np.pi, np.pi, size=count)
    elevation = rng.uniform(-0.45, 0.45, size=count)
    return np.stack(
        [
            np.cos(elevation) * np.sin(azimuth),
            np.sin(elevation),
            np.cos(elevation) * np.cos(azimuth),
        ],
        axis=1,
    )


def _rot_y(degrees: float) -> np.ndarray:
    theta = np.radians(degrees)
    return np.array(
        [[np.cos(theta), 0.0, np.sin(theta)], [0.0, 1.0, 0.0], [-np.sin(theta), 0.0, np.cos(theta)]]
    )


def _same_spot(yaw_deg: float = 25.0, count: int = POINTS) -> MatchResult:
    """What a genuine revisit looks like: one rotation explains every correspondence."""
    bearings = _bearings(count)
    rotated = bearings @ _rot_y(yaw_deg).T
    return MatchResult(
        points_a=bearings_to_pixels(bearings, WIDTH, HEIGHT),
        points_b=bearings_to_pixels(rotated, WIDTH, HEIGHT),
        raw_match_count=count,
        inlier_count=count,
    )


def _moved(distance_m: float, count: int = POINTS, seed: int = 7) -> MatchResult:
    """What the same corridor from a different spot looks like.

    The camera translates by ``distance_m`` through a scene whose points sit at varied depths, so
    each correspondence moves by a different amount — which is parallax, and is precisely the
    signal that no single rotation can absorb.
    """
    rng = np.random.default_rng(seed + 1)
    bearings = _bearings(count, seed)
    depths = rng.uniform(3.0, 12.0, size=(count, 1))
    world = bearings * depths
    translation = np.array([distance_m, 0.0, 0.0])
    moved = world - translation
    moved /= np.linalg.norm(moved, axis=1, keepdims=True)
    return MatchResult(
        points_a=bearings_to_pixels(bearings, WIDTH, HEIGHT),
        points_b=bearings_to_pixels(moved, WIDTH, HEIGHT),
        raw_match_count=count,
        inlier_count=count,
    )


def _clustered(count: int = POINTS) -> MatchResult:
    """Correspondences all on one patch of wall: zero parallax over nothing."""
    rng = np.random.default_rng(3)
    patch = np.column_stack([rng.uniform(900, 1000, size=count), rng.uniform(500, 520, size=count)])
    return MatchResult(
        points_a=patch, points_b=patch.copy(), raw_match_count=count, inlier_count=count
    )


def _features(seed: int = 0) -> FeatureSet:
    rng = np.random.default_rng(seed)
    return FeatureSet(
        keypoints=rng.uniform(0, WIDTH, size=(POINTS, 2)).astype(np.float32),
        descriptors=rng.integers(0, 255, size=(POINTS, 32), dtype=np.uint8),
        image_width=WIDTH,
        image_height=HEIGHT,
        projection=ProjectionType.EQUIRECTANGULAR,
    )


class StubMatcher:
    """Returns the answer the test scripted for each reference frame."""

    def __init__(self, results: dict[int, MatchResult], default: MatchResult | None = None) -> None:
        self.results = results
        self.default = default or MatchResult(
            np.zeros((0, 2)), np.zeros((0, 2)), raw_match_count=0, inlier_count=0
        )
        self.calls = 0

    def match(self, a: FeatureSet, b: FeatureSet) -> MatchResult:
        self.calls += 1
        return self.results.get(id(b), self.default)


def _reference(
    result_map: dict[int, MatchResult],
    match: MatchResult,
    *,
    frame_id: str = "F-old",
    plan: tuple[float, float] = (100.0, 100.0),
    digest: str = "ffffffffffffffff",
    location_id: str = "L-1",
) -> ReferenceFrame:
    """A placed frame from an earlier capture, scripted to produce ``match``."""
    features = _features(seed=abs(hash(frame_id)) % 1000)
    result_map[id(features)] = match
    return ReferenceFrame(
        frame_id=frame_id,
        session_id="CAP-EARLIER",
        plan_x=plan[0],
        plan_y=plan[1],
        perceptual_hash=digest,
        location_id=location_id,
        load_features=lambda: features,
    )


# ---- what counts as a position ----------------------------------------------
def test_a_frame_from_the_same_spot_is_recognised() -> None:
    """The capability itself: one rotation explains everything, so the place is the same."""
    results: dict[int, MatchResult] = {}
    reference = _reference(results, _same_spot())

    report = Relocalizer(StubMatcher(results)).recognise(
        [_features(1)], ["ffffffffffffffff"], [reference]
    )

    assert len(report.matches) == 1
    match = report.matches[0]
    assert match.plan_x == 100.0
    assert match.location_id == "L-1"
    assert match.parallax_deg < 0.1
    assert match.confidence > 0.0


def test_a_frame_taken_further_down_the_corridor_is_not_a_position() -> None:
    """The test the whole module exists to pass.

    A frame two metres away matches beautifully — same walls, same fittings, hundreds of
    inliers, a hash distance of nearly nothing. It is still not where the camera was, and
    accepting it would place the capture two metres out while calling the result a measurement.
    """
    results: dict[int, MatchResult] = {}
    reference = _reference(results, _moved(distance_m=2.0))

    report = Relocalizer(StubMatcher(results)).recognise(
        [_features(1)], ["ffffffffffffffff"], [reference]
    )

    assert report.matches == []
    assert report.rejected_parallax == 1


def test_appearance_alone_never_places_a_frame() -> None:
    """An identical hash with nothing verified behind it is a resemblance, not a position."""
    results: dict[int, MatchResult] = {}
    weak = MatchResult(
        points_a=np.zeros((4, 2)),
        points_b=np.zeros((4, 2)),
        raw_match_count=400,
        inlier_count=MIN_MATCH_INLIERS - 1,
    )
    reference = _reference(results, weak)

    report = Relocalizer(StubMatcher(results)).recognise(
        [_features(1)], ["ffffffffffffffff"], [reference]
    )

    assert report.matches == []
    assert report.rejected_few_inliers == 1


def test_correspondences_on_one_patch_are_refused() -> None:
    """Zero parallax measured across a blank patch of wall is arithmetic, not evidence."""
    results: dict[int, MatchResult] = {}
    reference = _reference(results, _clustered())

    report = Relocalizer(StubMatcher(results)).recognise(
        [_features(1)], ["ffffffffffffffff"], [reference]
    )

    assert report.matches == []
    assert report.rejected_clustered == 1


def test_a_frame_with_no_hash_is_never_compared() -> None:
    """The shortlist is what keeps recognition affordable; no hash means no shortlist."""
    results: dict[int, MatchResult] = {}
    reference = _reference(results, _same_spot())
    matcher = StubMatcher(results)

    report = Relocalizer(matcher).recognise([_features(1)], [""], [reference])

    assert report.matches == []
    assert matcher.calls == 0


def test_a_distant_hash_is_not_verified() -> None:
    """Shortlisting is loose, but it is not nothing: 0x0 against 0xffff is 64 bits apart."""
    results: dict[int, MatchResult] = {}
    reference = _reference(results, _same_spot(), digest="ffffffffffffffff")
    matcher = StubMatcher(results)

    report = Relocalizer(matcher).recognise([_features(1)], ["0000000000000000"], [reference])

    assert report.matches == []
    assert matcher.calls == 0


def test_the_nearest_viewpoint_wins_not_the_first_acceptable_one() -> None:
    """The off-by-one guard, and the reason every shortlisted candidate is verified.

    The tolerance is around a metre and capture points are often spaced about that far apart, so
    two adjacent reference points can both clear the bar. Taking the first would place the frame
    at its neighbour — and since every frame of the walk would shift the same way, the result is
    a pure translation that a similarity fit reproduces with a zero residual. Nothing downstream
    would ever see it. Parallax is the only thing here that orders candidates by distance.
    """
    results: dict[int, MatchResult] = {}
    further = _reference(
        results, _moved(1.0), frame_id="F-far", plan=(500.0, 100.0), location_id="L-far"
    )
    nearer = _reference(
        results, _moved(0.1), frame_id="F-near", plan=(100.0, 100.0), location_id="L-near"
    )
    matcher = StubMatcher(results)

    report = Relocalizer(matcher).recognise([_features(1)], ["ffffffffffffffff"], [further, nearer])

    assert len(report.matches) == 1
    assert report.matches[0].location_id == "L-near"
    assert matcher.calls == 2


def test_a_metre_of_movement_is_still_the_same_place() -> None:
    """The tolerance the feature needs to exist at all.

    Nobody re-stands where they stood last week. Held to loop closure's 0.3° — a centimetre or
    two — this would recognise nothing, and the operator would keep anchoring by hand forever.
    """
    results: dict[int, MatchResult] = {}
    reference = _reference(results, _moved(0.5))

    report = Relocalizer(StubMatcher(results)).recognise(
        [_features(1)], ["ffffffffffffffff"], [reference]
    )

    assert len(report.matches) == 1
    assert report.matches[0].parallax_deg < MAX_PARALLAX_DEG


def test_one_reference_cannot_anchor_two_frames() -> None:
    """Two frames of one walk were not both taken at one point.

    Allowing it would hand the fit two correspondences with the same target, which pins a
    translation and leaves rotation and scale to be decided by whatever noise remains.
    """
    results: dict[int, MatchResult] = {}
    reference = _reference(results, _same_spot())

    report = Relocalizer(StubMatcher(results)).recognise(
        [_features(1), _features(2)],
        ["ffffffffffffffff", "ffffffffffffffff"],
        [reference],
    )

    assert len(report.matches) == 1
    assert report.rejected_duplicate_reference == 1


def test_confidence_falls_as_parallax_rises() -> None:
    """Confidence has to track the evidence, or reporting it is decoration."""
    still = _place(parallax_deg=0.0)
    drifting = _place(parallax_deg=0.2)
    assert still.confidence > drifting.confidence


def _place(*, parallax_deg: float) -> PlaceMatch:
    from app.vision.relocalize import _confidence

    return PlaceMatch(
        query_index=0,
        reference_frame_id="F",
        reference_session_id="S",
        location_id="L",
        plan_x=0.0,
        plan_y=0.0,
        inlier_count=MIN_MATCH_INLIERS,
        parallax_deg=parallax_deg,
        hash_distance=0,
        yaw_offset_deg=0.0,
        confidence=_confidence(MIN_MATCH_INLIERS, parallax_deg, 0.3),
    )


# ---- what counts as enough to place a whole track ---------------------------
def _match(index: int, x: float, y: float) -> PlaceMatch:
    return PlaceMatch(
        query_index=index,
        reference_frame_id=f"F{index}",
        reference_session_id="S",
        location_id=f"L{index}",
        plan_x=x,
        plan_y=y,
        inlier_count=MIN_MATCH_INLIERS,
        parallax_deg=0.01,
        hash_distance=0,
        yaw_offset_deg=0.0,
        confidence=0.9,
    )


def test_two_matches_cannot_place_a_track() -> None:
    """A two-point similarity fit passes through both points exactly, whatever they are.

    Its residual is therefore zero by construction and says nothing about whether the anchors
    were right. Manual anchoring accepts two because a person looked at the plan; nothing looked
    at these.
    """
    usable, reason = anchors_are_usable([_match(0, 100, 100), _match(4, 900, 100)])

    assert usable is False
    assert str(MIN_TRANSFORM_MATCHES) in reason


def test_three_matches_spread_across_the_plan_can() -> None:
    usable, reason = anchors_are_usable(
        [_match(0, 100, 100), _match(4, 900, 140), _match(9, 500, 700)]
    )

    assert usable is True
    assert "3 frames recognised" in reason


def test_three_matches_on_top_of_each_other_cannot() -> None:
    """A cluster fixes where the walk was; it cannot say which way round it ran, or how big."""
    usable, reason = anchors_are_usable(
        [_match(0, 100, 100), _match(4, 110, 104), _match(9, 96, 112)]
    )

    assert usable is False
    assert "rotation and scale" in reason


@pytest.mark.parametrize("separation", [MIN_ANCHOR_SEPARATION_PX - 1, MIN_ANCHOR_SEPARATION_PX + 1])
def test_the_separation_bar_is_the_documented_constant(separation: float) -> None:
    usable, _ = anchors_are_usable(
        [_match(0, 0, 0), _match(1, separation, 0), _match(2, separation / 2, 0)]
    )

    assert usable is (separation > MIN_ANCHOR_SEPARATION_PX)


def test_nothing_to_match_against_is_not_an_error() -> None:
    """The floor's first capture. Every floor has one, and it must not look like a failure."""
    report = Relocalizer(StubMatcher({})).recognise([_features(1)], ["ffffffffffffffff"], [])

    assert report.matches == []
    assert report.references_considered == 0
