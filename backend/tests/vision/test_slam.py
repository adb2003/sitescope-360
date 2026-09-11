"""Ground-truth tests for pose-graph SLAM.

The claim SLAM makes is specific and measurable: *closing a loop reduces drift*. So these tests
build a walkthrough whose true shape is known, corrupt the odometry the way real odometry is
corrupted -- a small systematic yaw bias, which is what actually bends a track -- and measure the
error against truth before and after optimisation.

Deliberately free of ORB. The lesson from `test_mapping.py` was that routing a numeric assertion
through OpenCV's feature detector makes the bound platform-dependent; the solver is
deterministic, so its tests are exact everywhere. The front end's own behaviour is tested with
stub matchers for the same reason, and its integration with real imagery is covered by
`test_mapping.py`.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.core.enums import MappingConfidence, ProjectionType
from app.vision.position import DeadReckoningPositionEstimator
from app.vision.slam import (
    LOOP_INFORMATION_SCALE,
    MIN_LOOP_INLIERS,
    MIN_LOOP_SEPARATION,
    GraphSlamPositionEstimator,
    LoopClosure,
    LoopClosureDetector,
    OptimisationReport,
    PoseGraph,
    PoseGraphEdge,
    build_graph,
    hamming_distance,
)
from app.vision.types import FeatureSet, MatchResult, RelativeMotion, RelativePose

STEP_M = 2.0
SIDE_STEPS = 6  # 6 steps of 2 m per side -> a 12 m square, 24 poses around the loop


def square_loop() -> np.ndarray:
    """Ground-truth poses walking a closed square, heading tangent to the path."""
    poses = []
    x = y = 0.0
    for side, heading in enumerate((0.0, 90.0, 180.0, 270.0)):
        radians = math.radians(heading)
        for _ in range(SIDE_STEPS):
            poses.append([x, y, radians])
            x += STEP_M * math.cos(radians)
            y += STEP_M * math.sin(radians)
        del side
    return np.array(poses)


def rot_y(degrees: float) -> np.ndarray:
    """Rotation about the vertical axis matching the estimator's convention.

    ``_yaw_from_rotation`` reads yaw as ``atan2(R[0, 2], R[2, 2])``, so this is the matrix whose
    extracted yaw equals ``degrees``.
    """
    theta = math.radians(degrees)
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def drifted_odometry(truth: np.ndarray, yaw_bias_deg: float) -> list[RelativeMotion]:
    """Relative motions for the walk, with a constant yaw error added to each step.

    A constant bias is the honest choice: zero-mean noise partly cancels over a loop, so it would
    flatter the result. A systematic bias is both more damaging and closer to what a real
    estimator does when its geometry is slightly off.

    The bias goes into the rotation matrix *and* ``yaw_deg`` together. That is not a detail --
    :meth:`DeadReckoningPositionEstimator.integrate` rotates the travel direction into world
    using the accumulated ``motion.rotation``, while reporting heading from accumulated
    ``yaw_deg``. An earlier version of this helper left ``rotation`` as the identity, so the
    reported heading turned through the corners while the track walked dead straight through
    them -- a 46 m "drift" that was an artefact of the fixture, not of any estimator.

    The operator always walks along their current heading, so in the camera's own frame the
    travel direction is a constant (1, 0, 0).
    """
    motions = []
    for index in range(len(truth) - 1):
        true_turn = math.degrees(_wrap(truth[index + 1][2] - truth[index][2]))
        observed_turn = true_turn + yaw_bias_deg
        motions.append(
            RelativeMotion(
                # (x, up, z): the estimator reads indices 0 and 2 as the floor plane.
                translation=np.array([1.0, 0.0, 0.0]),
                rotation=rot_y(observed_turn),
                yaw_deg=observed_turn,
                confidence=0.9,
                inlier_count=120,
                match_count=140,
            )
        )
    return motions


def dead_reckoned(truth: np.ndarray, yaw_bias_deg: float):
    motions = drifted_odometry(truth, yaw_bias_deg)
    poses = DeadReckoningPositionEstimator().integrate(motions, [STEP_M] * len(motions))
    return poses, motions


def closing_observation(truth: np.ndarray) -> LoopClosure:
    """A perfect closure tying the last pose back to the first.

    This is what the front end produces when a revisit is verified; its own correctness is tested
    separately. Here the back end is under test, so the observation is exact.
    """
    last = len(truth) - 1
    delta = truth[0][:2] - truth[last][:2]
    theta = truth[last][2]
    local = np.array(
        [
            math.cos(-theta) * delta[0] - math.sin(-theta) * delta[1],
            math.sin(-theta) * delta[0] + math.cos(-theta) * delta[1],
        ]
    )
    norm = float(np.linalg.norm(local)) or 1.0
    return LoopClosure(
        from_index=last,
        to_index=0,
        dx=float(local[0] / norm),
        dy=float(local[1] / norm),
        dtheta_deg=math.degrees(_wrap(truth[0][2] - theta)),
        inlier_count=90,
        hash_distance=4,
        confidence=0.9,
    )


def _wrap(angle: float) -> float:
    return float(np.arctan2(np.sin(angle), np.cos(angle)))


def endpoint_gap(poses: list[RelativePose]) -> float:
    """How far the track fails to close, in track units. This is the drift SLAM must remove."""
    first, last = poses[0], poses[-1]
    return float(math.hypot(last.x - first.x, last.y - first.y))


# ---- the claim ---------------------------------------------------------------
def shape_error(poses: list[RelativePose] | np.ndarray, truth: np.ndarray) -> float:
    """Mean distance to ground truth after the similarity fit floor-plan anchoring applies.

    This is the metric that matters. The track is scale-free, so absolute coordinates mean
    nothing until anchored; what SLAM must improve is the *shape*.
    """
    from app.vision.mapbuilder import MapBuilder

    if isinstance(poses, np.ndarray):
        estimated = np.array(poses[:, :2])
    else:
        estimated = np.array([[pose.x, pose.y] for pose in poses])
    target = truth[:, :2]
    fit = MapBuilder().fit_transform(estimated, target)
    assert fit is not None
    return float(np.linalg.norm(fit.apply(estimated) - target, axis=1).mean())


def test_drift_is_real_before_anything_is_closed() -> None:
    """The premise. Without this the rest of the file proves nothing.

    Stated against ground-truth shape rather than the endpoint gap. A yaw bias skews the whole
    square, and 1.5 deg/step over 23 steps leaves the endpoint only ~1.2 units out of place while
    bending the shape far more -- so the gap alone understates the damage.
    """
    truth = square_loop()
    poses, _ = dead_reckoned(truth, yaw_bias_deg=1.5)

    error = shape_error(poses, truth)
    print(f"\ndrifted shape error: {error:.3f} track units")
    assert error > 0.25 * STEP_M, (
        f"the odometry barely drifted ({error:.3f}), so the tests below would be vacuous"
    )

    # And the loop genuinely fails to close: the gap exceeds the true one by a real margin.
    true_gap = float(np.linalg.norm(truth[-1][:2] - truth[0][:2]))
    assert endpoint_gap(poses) > true_gap * 1.25


def test_closing_the_loop_recovers_the_true_endpoint_gap() -> None:
    """A bearing-only closure cannot squeeze the gap to zero -- and must not.

    The last pose is genuinely one step from the first, so a solver that drove the gap to zero
    would be inventing a revisit that did not happen. The correct outcome is the *true* gap, and
    that is what is asserted: the optimised gap lands near 2.0 units, not near 0.

    This is the assertion an earlier version got wrong. It demanded a 50% reduction, which the
    metric-displacement formulation happily delivered by pulling the endpoints to exactly 1.0
    units apart -- the unit length of a translation that carries no distance information at all.
    """
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.5)
    before = endpoint_gap(poses)
    true_gap = float(np.linalg.norm(truth[-1][:2] - truth[0][:2]))

    graph = build_graph(poses, motions, [closing_observation(truth)])
    report = graph.optimise()

    assert report.converged, report.notes
    assert report.loop_closures_used == 1

    after = math.hypot(
        graph.poses[-1, 0] - graph.poses[0, 0], graph.poses[-1, 1] - graph.poses[0, 1]
    )
    print(f"\nendpoint gap: {before:.2f} -> {after:.2f} track units (true gap {true_gap:.2f})")
    assert after < before, f"the gap did not improve: {before:.2f} -> {after:.2f}"
    assert abs(after - true_gap) < 0.5 * STEP_M, (
        f"optimised gap {after:.2f} is not the true gap {true_gap:.2f}"
    )


def test_optimisation_moves_the_whole_track_closer_to_truth() -> None:
    """Endpoint gap alone is not enough.

    A solver could close the gap by folding the track onto itself and look successful while making
    the shape worse. This measures every pose against ground truth, after the same similarity fit
    that floor-plan anchoring applies, because the track is scale-free.
    """
    from app.vision.mapbuilder import MapBuilder

    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.5)
    target = truth[:, :2]

    builder = MapBuilder()
    raw = np.array([[pose.x, pose.y] for pose in poses])
    fit_before = builder.fit_transform(raw, target)
    assert fit_before is not None
    error_before = np.linalg.norm(fit_before.apply(raw) - target, axis=1).mean()

    graph = build_graph(poses, motions, [closing_observation(truth)])
    graph.optimise()

    optimised = np.array(graph.poses[:, :2])
    fit_after = builder.fit_transform(optimised, target)
    assert fit_after is not None
    error_after = np.linalg.norm(fit_after.apply(optimised) - target, axis=1).mean()

    print(f"\nmean error vs truth: {error_before:.3f} -> {error_after:.3f} track units")
    assert error_after < error_before, (
        f"the shape did not improve: {error_before:.3f} -> {error_after:.3f}"
    )


def test_an_open_walk_is_left_exactly_as_it_was() -> None:
    """With no closure the odometry chain *is* the optimum, and the solver must say so.

    Reporting a converged solve here would be misleading: there is nothing to reconcile, and a
    track that changed would mean the solver had invented information.
    """
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.5)

    graph = build_graph(poses, motions, [])
    before = graph.poses.copy()
    report = graph.optimise()

    assert report.loop_closures_used == 0
    assert report.max_correction == 0.0
    assert np.allclose(graph.poses, before)
    assert any("no loop closures" in note for note in report.notes), report.notes


def test_a_perfect_track_is_not_disturbed() -> None:
    """Zero drift plus a true closure must leave the track where it is."""
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=0.0)

    graph = build_graph(poses, motions, [closing_observation(truth)])
    before = graph.poses.copy()
    report = graph.optimise()

    assert report.converged, report.notes
    moved = np.linalg.norm(graph.poses[:, :2] - before[:, :2], axis=1).max()
    assert moved < 0.5 * STEP_M, f"an already-consistent track was moved {moved:.3f}"


def test_the_first_pose_is_never_moved() -> None:
    """The graph is anchored at node 0; without that the normal equations are singular."""
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=2.0)

    graph = build_graph(poses, motions, [closing_observation(truth)])
    graph.optimise()

    assert np.allclose(graph.poses[0], [poses[0].x, poses[0].y, math.radians(poses[0].heading_deg)])


def test_optimisation_never_returns_a_worse_track() -> None:
    """A contradictory closure must be rejected, not accommodated.

    A closure claiming the loop ends 180 degrees from where the odometry puts it is the shape of a
    false positive from the front end. The optimiser may fail to improve things -- it must not
    hand back something worse than it was given.
    """
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.5)

    honest = closing_observation(truth)
    contradictory = LoopClosure(
        from_index=honest.from_index,
        to_index=honest.to_index,
        dx=-honest.dx,
        dy=-honest.dy,
        dtheta_deg=honest.dtheta_deg + 180.0,
        inlier_count=honest.inlier_count,
        hash_distance=honest.hash_distance,
        confidence=honest.confidence,
    )

    graph = build_graph(poses, motions, [contradictory])
    report = graph.optimise()

    assert report.final_error <= report.initial_error + 1e-9, (
        f"error grew: {report.initial_error:.4f} -> {report.final_error:.4f}"
    )


# ---- the front end -----------------------------------------------------------
def test_hamming_distance_counts_bits_and_survives_rubbish() -> None:
    assert hamming_distance("00", "00") == 0
    assert hamming_distance("00", "ff") == 8
    assert hamming_distance("0f", "00") == 4
    # A malformed hash must not shortlist a closure, and must not raise into the pipeline.
    assert hamming_distance("not-a-hash", "00") == 64
    assert hamming_distance("", "00") == 64


def test_adjacent_frames_are_never_loop_candidates() -> None:
    """Neighbouring frames always look alike; that is odometry, already in the graph."""
    detector = LoopClosureDetector(_StubMatcher(0), _StubMotion(reliable=False))
    identical = ["00"] * (MIN_LOOP_SEPARATION + 3)

    pairs = detector.candidates(identical)

    assert pairs, "identical frames far apart should still be candidates"
    assert all(j - i >= MIN_LOOP_SEPARATION for i, j, _ in pairs)


def test_appearance_alone_never_closes_a_loop() -> None:
    """The central safeguard.

    A construction site is full of places that look alike -- identical bays, repeated formwork.
    Accepting a closure on appearance would fold a corridor onto itself, and the optimiser would
    then bend the whole track to honour it.
    """
    count = MIN_LOOP_SEPARATION + 4
    features = [_stub_features() for _ in range(count)]
    hashes = ["00"] * count  # every frame looks identical

    # Plenty of matches, but the geometry refuses.
    detector = LoopClosureDetector(_StubMatcher(200), _StubMotion(reliable=False))
    assert detector.detect(features, hashes) == []

    # Geometry agrees, but there are too few inliers to trust.
    detector = LoopClosureDetector(_StubMatcher(MIN_LOOP_INLIERS - 1), _StubMotion(reliable=True))
    assert detector.detect(features, hashes) == []

    # Both hold up: now it is a closure.
    detector = LoopClosureDetector(_StubMatcher(200), _StubMotion(reliable=True))
    assert detector.detect(features, hashes)


def test_detect_refuses_mismatched_inputs() -> None:
    detector = LoopClosureDetector(_StubMatcher(200), _StubMotion(reliable=True))
    with pytest.raises(ValueError, match="parallel"):
        detector.detect([_stub_features()], ["00", "01"])


# ---- the estimator -----------------------------------------------------------
def test_a_short_walk_is_returned_untouched() -> None:
    estimator = GraphSlamPositionEstimator(
        DeadReckoningPositionEstimator(),
        LoopClosureDetector(_StubMatcher(200), _StubMotion(reliable=True)),
    )
    truth = square_loop()[:4]
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.0)

    outcome = estimator.refine(poses, motions, [_stub_features()] * len(poses), ["00"] * len(poses))

    assert outcome.unchanged
    assert outcome.poses is poses
    assert "too short" in outcome.report.notes[0]


def test_no_verified_revisit_leaves_the_track_alone() -> None:
    estimator = GraphSlamPositionEstimator(
        DeadReckoningPositionEstimator(),
        LoopClosureDetector(_StubMatcher(200), _StubMotion(reliable=False)),
    )
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.5)

    outcome = estimator.refine(poses, motions, [_stub_features()] * len(poses), ["00"] * len(poses))

    assert outcome.unchanged
    assert outcome.closures == []
    assert outcome.to_dict()["method"] == "dead_reckoning"


def test_the_outcome_reports_which_method_produced_it() -> None:
    """§46: a consumer must be able to tell an optimised track from a dead-reckoned one."""
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.5)
    graph = build_graph(poses, motions, [closing_observation(truth)])
    report = graph.optimise()

    payload = report.to_dict()
    assert payload["loop_closures_used"] == 1
    assert 0.0 <= payload["error_reduction"] <= 1.0
    assert payload["converged"] is True


def test_loop_edges_outweigh_a_single_step_but_not_the_whole_chain() -> None:
    """The weighting that keeps one closure from overpowering the odometry."""
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.5)
    graph = build_graph(poses, motions, [closing_observation(truth)])

    loop_edges = [edge for edge in graph.edges if edge.is_loop]
    odometry_edges = [edge for edge in graph.edges if not edge.is_loop]

    assert len(loop_edges) == 1
    assert loop_edges[0].info_theta > odometry_edges[0].info_theta
    assert loop_edges[0].info_theta < sum(edge.info_theta for edge in odometry_edges)
    # For a loop edge `info_xy` weights the *bearing* residual, not a displacement. Bearing and
    # heading are both angles from the same estimate, so they carry equal weight -- there is no
    # fabricated distance left to discount, as there was in the first version of this code.
    assert loop_edges[0].info_xy == loop_edges[0].info_theta


def test_an_edge_outside_the_graph_is_refused() -> None:
    graph = PoseGraph(np.zeros((3, 3)))
    with pytest.raises(ValueError, match="outside graph"):
        graph.add_edge(PoseGraphEdge(0, 7, 0.0, 0.0, 0.0, 1.0, 1.0))


def test_information_scale_is_a_documented_constant() -> None:
    """Guards the constant the weighting test above depends on."""
    assert LOOP_INFORMATION_SCALE > 1.0


# ---- stubs -------------------------------------------------------------------
def _stub_features() -> FeatureSet:
    return FeatureSet(
        keypoints=np.zeros((10, 2)),
        descriptors=np.zeros((10, 32), dtype=np.uint8),
        image_width=1600,
        image_height=800,
        projection=ProjectionType.EQUIRECTANGULAR,
    )


class _StubMatcher:
    """Returns a fixed inlier count, so threshold behaviour is what is being tested."""

    def __init__(self, inliers: int) -> None:
        self.inliers = inliers

    def match(self, a: FeatureSet, b: FeatureSet) -> MatchResult:
        points = np.zeros((max(self.inliers, 1), 2))
        # MatchResult takes raw_match_count before inlier_count.
        return MatchResult(points, points, max(self.inliers, 1), self.inliers)


class _StubMotion:
    """Stands in for the essential-matrix estimator with a fixed verdict."""

    def __init__(self, *, reliable: bool) -> None:
        self.reliable = reliable

    def estimate(self, matches: MatchResult, width: int, height: int) -> RelativeMotion:
        return RelativeMotion(
            translation=np.array([1.0, 0.0, 0.0]),
            rotation=np.eye(3),
            yaw_deg=5.0,
            confidence=0.8 if self.reliable else 0.0,
            inlier_count=matches.inlier_count if self.reliable else 0,
            match_count=matches.raw_match_count,
        )


def test_mapping_confidence_enum_is_used_not_a_string() -> None:
    """Cheap guard: the outcome must carry the shared enum, not a free-text label."""
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.0)
    estimator = GraphSlamPositionEstimator(
        DeadReckoningPositionEstimator(),
        LoopClosureDetector(_StubMatcher(0), _StubMotion(reliable=False)),
    )
    outcome = estimator.refine(
        poses,
        motions,
        [_stub_features()] * len(poses),
        ["00"] * len(poses),
        base_confidence=MappingConfidence.HIGH,
    )
    assert outcome.confidence is MappingConfidence.HIGH


# ---- derivatives -------------------------------------------------------------
@pytest.mark.parametrize("is_loop", [False, True])
def test_analytic_jacobians_match_finite_differences(is_loop: bool) -> None:
    """A wrong Jacobian does not fail loudly -- it converges slowly to somewhere plausible.

    Both edge kinds are checked because they have different residual dimensions and the loop
    edge's bearing row goes through an atan2 chain rule, which is exactly the sort of derivative
    that is easy to get subtly wrong.
    """
    rng = np.random.default_rng(11)
    for _ in range(5):
        poses = rng.uniform(-4.0, 4.0, size=(2, 3))
        edge = PoseGraphEdge(0, 1, 0.6, 0.8, 0.3, 1.0, 1.0, is_loop=is_loop)
        graph = PoseGraph(poses)
        graph.add_edge(edge)

        analytic = graph._jacobians(edge)
        dimension = analytic[0].shape[0]
        assert dimension == (2 if is_loop else 3)

        epsilon = 1e-7
        for node in (0, 1):
            numeric = np.zeros((dimension, 3))
            for axis in range(3):
                up, down = graph.poses.copy(), graph.poses.copy()
                up[node, axis] += epsilon
                down[node, axis] -= epsilon
                graph_up, graph_down = PoseGraph(up), PoseGraph(down)
                graph_up.add_edge(edge)
                graph_down.add_edge(edge)
                numeric[:, axis] = (graph_up._residual(edge) - graph_down._residual(edge)) / (
                    2 * epsilon
                )

            assert np.allclose(analytic[node], numeric, atol=1e-5), (
                f"node {node} derivative disagrees:\n{analytic[node]}\nvs\n{numeric}"
            )


def test_a_bearing_closure_carries_no_distance_information() -> None:
    """The property that makes the bearing formulation correct.

    Scaling both poses away from the origin changes the distance between them but not the bearing
    from one to the other, so a bearing-only residual must be unchanged. A metric-displacement
    residual would move, which is precisely the fabricated measurement this replaced.
    """
    near = np.array([[0.0, 0.0, 0.0], [3.0, 4.0, 0.4]])
    far = np.array([[0.0, 0.0, 0.0], [30.0, 40.0, 0.4]])
    edge = PoseGraphEdge(0, 1, 0.6, 0.8, 0.4, 1.0, 1.0, is_loop=True)

    graph_near, graph_far = PoseGraph(near), PoseGraph(far)
    graph_near.add_edge(edge)
    graph_far.add_edge(edge)

    assert np.allclose(graph_near._residual(edge), graph_far._residual(edge))


# ---- outlier closures --------------------------------------------------------
def test_contradictory_closures_are_rejected_and_reported() -> None:
    """The fix for a measured regression, pinned.

    On a rendered rectangular circuit the detector accepted twelve "verified" closures, two of
    them between genuinely different corners of the room, and the optimised track came out 24%
    *worse* than plain dead reckoning -- while reporting convergence, a 97.9% drop in constraint
    error, and high confidence. Every internal signal said success.

    Repetitive wall texture yields both a high ORB inlier count and a self-consistent essential
    matrix between places that merely look alike, so no inlier threshold alone catches it. What
    catches it is asking, after a robust solve, which constraints still disagree with everything
    else. After this fix the same circuit improved 38% over odometry instead of degrading.
    """
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.5)

    honest = closing_observation(truth)
    # Two mutually incompatible claims about the same pair: at most one can be satisfied, so a
    # large residual must survive the solve.
    conflicting = [
        LoopClosure(
            from_index=honest.from_index,
            to_index=honest.to_index,
            dx=-honest.dx,
            dy=-honest.dy,
            dtheta_deg=honest.dtheta_deg + 180.0,
            inlier_count=95,
            hash_distance=6,
            confidence=0.9,
        ),
        honest,
    ]

    graph = build_graph(poses, motions, conflicting)
    report = graph.optimise()

    assert report.loop_closures_rejected >= 1, report.notes
    assert any("rejected" in note for note in report.notes), report.notes


def test_a_lone_absorbable_outlier_is_a_known_limitation() -> None:
    """Documented honestly rather than papered over.

    A *single* false closure that the graph can satisfy by deforming the track is not detectable
    by these checks: the optimiser bends the poses until the residual vanishes, so there is no
    surviving disagreement to reject. It is caught only when it contradicts something else.

    A cluster-support filter was tried -- requiring each closure to be corroborated by another
    with nearby endpoints, which is what the real data showed distinguished true from false. It
    worked on this case and measurably *hurt* the real one, cutting the rendered circuit's
    improvement from 38% to 12% by discarding true closures along with false. It was removed: a
    filter that costs accuracy on real imagery to satisfy a synthetic case is the wrong trade.

    So this test asserts the limitation exists, which is the honest state of the feature. The
    defences that remain are a high inlier bar, the Huber kernel, and residual rejection, and
    describe_capabilities() states that a single unchallenged false closure can still distort a
    track.
    """
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.5)
    lone_outlier = LoopClosure(
        from_index=6,
        to_index=18,
        dx=0.0,
        dy=1.0,
        dtheta_deg=170.0,
        inlier_count=95,
        hash_distance=6,
        confidence=0.9,
    )

    graph = build_graph(poses, motions, [lone_outlier])
    report = graph.optimise()

    # Absorbed, not rejected -- the residual is driven to nothing by moving the poses.
    assert report.loop_closures_rejected == 0
    # The one guarantee that must still hold: never worse than what it was given.
    assert report.final_error <= report.initial_error + 1e-9


def test_a_huge_outlier_cannot_outvote_the_odometry() -> None:
    """Why the Huber kernel exists.

    Plain least squares weights a constraint by the *square* of how wrong it is, so one false
    closure outvotes the whole odometry chain. With the robust kernel its influence grows only
    linearly past the threshold. Checked with rejection disabled, so the kernel is what is under
    test rather than the rejection pass hiding it.
    """
    truth = square_loop()
    poses, motions = dead_reckoned(truth, yaw_bias_deg=1.5)
    absurd = LoopClosure(
        from_index=2,
        to_index=20,
        dx=-1.0,
        dy=0.0,
        dtheta_deg=180.0,
        inlier_count=99,
        hash_distance=1,
        confidence=1.0,
    )

    graph = build_graph(poses, motions, [absurd])
    graph.optimise(reject_outliers=False)

    # The track may be distorted, but it must not have been folded in half to satisfy one edge.
    span_truth = float(np.ptp(truth[:, 0]) + np.ptp(truth[:, 1]))
    span_result = float(np.ptp(graph.poses[:, 0]) + np.ptp(graph.poses[:, 1]))
    assert span_result > 0.4 * span_truth, (
        f"one outlier collapsed the track: span {span_result:.1f} vs {span_truth:.1f}"
    )


def test_the_report_cannot_be_built_positionally() -> None:
    """Guards the trap that caused a silent bug.

    Adding `loop_closures_rejected` mid-signature shifted `notes` into an int field at six call
    sites. Nothing failed there; the only symptom was an empty notes list in a test three files
    away. Seven same-shaped fields must be keyword-only.
    """
    with pytest.raises(TypeError):
        OptimisationReport(True, 0, 0.0, 0.0, 0.0, 0, [])  # type: ignore[misc]
