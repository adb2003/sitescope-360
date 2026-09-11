"""Graph SLAM: loop closure detection and SE(2) pose-graph optimisation (§15, §41 Phase 2).

Dead reckoning (:class:`app.vision.position.DeadReckoningPositionEstimator`) chains relative
motions head to tail. Every estimate carries error, and chaining accumulates it, so a walkthrough
that returns to where it started does not close: the track ends metres from its own beginning and
the whole shape is skewed. No amount of care in the two-view geometry fixes that, because the
information needed is not in any single pair — it is in the fact that frame 40 and frame 1 are
looking at *the same place*.

That observation is what this module adds, and it is what makes the difference between odometry
and SLAM:

    front end   find revisits    LoopClosureDetector
    back end    reconcile them   PoseGraph + optimise()

The back end is a Gauss-Newton solver on SE(2). Planar motion is already assumed everywhere else
in the platform (walkthroughs happen on a floor), so poses are (x, y, theta) and a full SE(3)
bundle adjustment would add unknowns the data cannot constrain.

**What this is not.** There is no landmark map, no global bundle adjustment over 3D points, and no
relocalisation against a prior session. This is pose-graph SLAM over one walkthrough. Loop closure
is verified geometrically, never on appearance alone, and a closure that fails verification is
discarded rather than added with a low weight -- a wrong closure is far more damaging than a
missing one, because the optimiser will bend the entire track to satisfy it.

Nothing here invents precision. :func:`optimise` reports how much it moved the track and whether
it converged, the estimator records which closures it used, and positions remain
``DataProvenance.ESTIMATED`` exactly as before (§46).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from app.core.enums import MappingConfidence
from app.vision.motion import _rotation_only_fit, _yaw_from_rotation
from app.vision.panorama import pixels_to_bearings
from app.vision.types import FeatureSet, MatchResult, RelativeMotion, RelativePose

#: Minimum frames between two keyframes before a match counts as a loop, not just overlap with a
#: neighbour. Adjacent frames always look alike; that is odometry, and it is already in the graph.
MIN_LOOP_SEPARATION = 8

#: Verified inliers a candidate closure needs. Far higher than the odometry threshold (20) on
#: purpose: a false closure corrupts the whole track, a missed one costs only the drift it would
#: have removed, so the asymmetry belongs in the threshold.
#:
#: Raised from 35 after measurement. On a rendered rectangular circuit, 35 admitted two closures
#: between genuinely different corners of the room -- one of them clearing the bar by a single
#: inlier -- and the resulting track was 24% *worse* than plain dead reckoning. Repetitive wall
#: texture produces both a high inlier count and a self-consistent essential matrix between
#: places that merely look alike, so the inlier count alone was never going to be enough; see
#: `RESIDUAL_OUTLIER_RADIANS` for the check that actually catches them.
MIN_LOOP_INLIERS = 60

#: Hamming distance between 64-bit dHashes below which two frames are worth verifying. Loose by
#: design -- this stage only shortlists; geometry decides.
MAX_HASH_DISTANCE = 22

#: Levenberg-Marquardt iteration limit. Convergence is normally reached in well under twenty.
MAX_ITERATIONS = 120

#: Initial LM damping, as a multiple of the Hessian diagonal. Small enough that a well-conditioned
#: graph takes near-Gauss-Newton steps immediately.
INITIAL_DAMPING = 1e-4
#: Damping is multiplied by this after a rejected step and divided by it after an accepted one.
DAMPING_FACTOR = 10.0
#: Past this the step is pure gradient descent at a vanishing rate; no further value is available.
MAX_DAMPING = 1e8
MIN_DAMPING = 1e-9

#: Below this separation two poses have no meaningful bearing between them, so a bearing-only
#: closure contributes no positional information rather than a direction read off rounding error.
_MIN_BEARING_DISTANCE = 1e-6

#: Stop when the largest pose correction falls below this (metres / radians).
CONVERGENCE_DELTA = 1e-6

#: Relative weight of a loop-closure constraint against one odometry step. A verified closure
#: observes two poses directly rather than through a chain, so it is worth more than a single
#: step -- but not so much more that one bad closure can overwhelm the odometry that disagrees.
LOOP_INFORMATION_SCALE = 4.0

#: Extra inliers a co-located closure needs, as a multiple of the bearing-closure bar. It
#: asserts position outright rather than a direction, so it must be better evidenced.
COLOCATED_INLIER_MULTIPLIER = 1.5

#: Parallax below which a pair is read as co-located rather than merely nearby. A few degrees
#: means "close to"; asserting an exact revisit from that would inject real error as fact.
MAX_COLOCATED_PARALLAX_DEG = 0.30

#: Fraction of the frame the correspondences must span before a co-located verdict is allowed.
#: Panoramas see all around, so a genuine revisit matches features across most of the width.
MIN_COLOCATED_SPREAD_X = 0.35
MIN_COLOCATED_SPREAD_Y = 0.10

#: Inliers at which a co-located closure's support term saturates.
SATURATION_LOOP_INLIERS = 150.0

#: Huber threshold for loop-closure residuals, in radians (~11.5 deg).
#:
#: Beyond this a closure's influence grows linearly rather than quadratically, so one badly wrong
#: constraint cannot dominate the solution. Least squares without a robust kernel gives an outlier
#: weight proportional to the square of how wrong it is, which is precisely backwards.
HUBER_DELTA_RADIANS = 0.20

#: After a robust solve, a loop edge still disagreeing by more than this (~28.6 deg) is rejected
#: as a false positive and the graph is solved again without it.
#:
#: The reasoning: a *true* revisit disagrees with the odometry by the accumulated drift, which the
#: optimiser can absorb by distributing it around the loop. A false revisit disagrees with the
#: rest of the graph in a way nothing can absorb, so a large surviving residual is the signature
#: of a wrong constraint rather than of a drifted one.
RESIDUAL_OUTLIER_RADIANS = 0.50


@dataclass(frozen=True, slots=True)
class LoopClosure:
    """A verified observation that two non-adjacent keyframes see the same place."""

    from_index: int
    to_index: int
    #: Relative pose of ``to_index`` in ``from_index``'s frame. For a co-located closure both are
    #: zero, which is the assertion being made: the two frames are in the same place.
    dx: float
    dy: float
    dtheta_deg: float
    inlier_count: int
    hash_distance: int
    confidence: float
    #: True when the operator returned to the *same spot* rather than merely nearby.
    #:
    #: Such a pair has no parallax, so the essential matrix is degenerate and the motion
    #: estimator refuses it -- correctly, because there is no translation to recover. That does
    #: not make the pair useless; it makes it the strongest closure available. Zero parallax with
    #: a large inlier count is positive evidence of co-location, and the heading change still
    #: comes out of the rotation-only fit.
    #:
    #: Missing this case meant loop closure found nothing on the most natural walkthrough there
    #: is: walk the floor and come back to where you began.
    is_colocated: bool = False

    def to_dict(self) -> dict[str, float | int | str]:
        return {
            "from_index": self.from_index,
            "to_index": self.to_index,
            "dtheta_deg": round(self.dtheta_deg, 2),
            "inlier_count": self.inlier_count,
            "hash_distance": self.hash_distance,
            "confidence": round(self.confidence, 3),
            "kind": "colocated" if self.is_colocated else "bearing",
        }


@dataclass(frozen=True, slots=True)
class PoseGraphEdge:
    """A relative-pose constraint: pose ``j`` should sit at ``(dx, dy, dtheta)`` from pose ``i``."""

    i: int
    j: int
    dx: float
    dy: float
    dtheta: float  # radians
    #: Diagonal information (inverse variance) for x, y and theta. Higher means "trust this more".
    info_xy: float
    info_theta: float
    #: True for any closure edge: subject to the Huber kernel and to outlier rejection.
    is_loop: bool = False
    #: True when the closure asserts *position* (same place) rather than a bearing. Its residual
    #: is a full relative pose like odometry's; only its weighting is that of a closure.
    is_colocated: bool = False


@dataclass(slots=True, kw_only=True)
class OptimisationReport:
    """What the solver did -- reported rather than assumed, so callers can refuse the result.

    Keyword-only, deliberately. Inserting ``loop_closures_rejected`` into the middle of a
    positional signature silently shifted ``notes`` into an int field at six call sites, and not
    one of them failed -- the only symptom was an empty notes list surfacing in a test three
    files away. Seven same-shaped fields should never have been positional.
    """

    converged: bool
    iterations: int
    initial_error: float
    final_error: float
    #: Largest distance any pose moved, in the same units as the track (scale-free).
    max_correction: float
    #: The same correction as a multiple of the track's median step.
    #:
    #: Raw track units are unreadable on their own: the pipeline derives step length from frame
    #: timestamps and an assumed pace, so a demo capture 12 s apart has steps of ~17 units and a
    #: correction of "108" is six steps, not 108 metres. Reporting it as a step multiple is the
    #: only form a reader can judge without knowing the scale.
    max_correction_steps: float = 0.0
    loop_closures_used: int
    #: Closures dropped as outliers after the robust solve. Reported, never silently discarded.
    loop_closures_rejected: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def error_reduction(self) -> float:
        """Fraction of squared constraint error removed, 0–1."""
        if self.initial_error <= 0.0:
            return 0.0
        return max(0.0, min(1.0, 1.0 - self.final_error / self.initial_error))

    def to_dict(self) -> dict[str, object]:
        return {
            "converged": self.converged,
            "iterations": self.iterations,
            "initial_error": round(self.initial_error, 6),
            "final_error": round(self.final_error, 6),
            "error_reduction": round(self.error_reduction, 4),
            "max_correction": round(self.max_correction, 4),
            "max_correction_steps": round(self.max_correction_steps, 2),
            "loop_closures_used": self.loop_closures_used,
            "loop_closures_rejected": self.loop_closures_rejected,
            "notes": list(self.notes),
        }


# ---- SE(2) helpers -----------------------------------------------------------
def _wrap(angle: float) -> float:
    """Wrap an angle to (-pi, pi]. Every residual on theta must pass through this.

    Without it a 359-degree error reads as enormous instead of as one degree, and Gauss-Newton
    chases the wrap rather than the pose.
    """
    return float(np.arctan2(np.sin(angle), np.cos(angle)))


def _rotation(theta: float) -> np.ndarray:
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, -s], [s, c]])


def _relative(pose_i: np.ndarray, pose_j: np.ndarray) -> np.ndarray:
    """Pose ``j`` expressed in pose ``i``'s frame."""
    delta = _rotation(-pose_i[2]) @ (pose_j[:2] - pose_i[:2])
    return np.array([delta[0], delta[1], _wrap(pose_j[2] - pose_i[2])])


# ---- front end: finding revisits --------------------------------------------
def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Bit difference between two hex dHash strings, or a large number if unusable.

    Returns a deliberately large value rather than raising on malformed input: a hash that cannot
    be compared must not shortlist a closure, and a corrupt stored hash should degrade the loop
    search, not fail the pipeline.
    """
    try:
        a, b = int(hash_a, 16), int(hash_b, 16)
    except (TypeError, ValueError):
        return 64
    return int(bin(a ^ b).count("1"))


class LoopClosureDetector:
    """Finds and geometrically verifies revisits between non-adjacent keyframes.

    Appearance shortlists, geometry decides. Perceptual hashing alone cannot tell a revisit from
    two structurally similar places -- and a construction site is full of those: identical bays,
    repeated formwork, parallel corridors. Accepting a closure on appearance would reliably fold
    a long corridor onto itself.
    """

    def __init__(
        self,
        matcher,
        motion_estimator,
        *,
        min_separation: int = MIN_LOOP_SEPARATION,
        min_inliers: int = MIN_LOOP_INLIERS,
        max_hash_distance: int = MAX_HASH_DISTANCE,
    ) -> None:
        self.matcher = matcher
        self.motion_estimator = motion_estimator
        self.min_separation = min_separation
        self.min_inliers = min_inliers
        self.max_hash_distance = max_hash_distance

    def candidates(self, hashes: list[str]) -> list[tuple[int, int, int]]:
        """Shortlist ``(i, j, hash_distance)`` pairs worth verifying, closest-looking first."""
        found: list[tuple[int, int, int]] = []
        for i in range(len(hashes)):
            for j in range(i + self.min_separation, len(hashes)):
                distance = hamming_distance(hashes[i], hashes[j])
                if distance <= self.max_hash_distance:
                    found.append((i, j, distance))
        found.sort(key=lambda item: item[2])
        return found

    def detect(
        self,
        features: list[FeatureSet],
        hashes: list[str],
        *,
        max_closures: int = 12,
    ) -> list[LoopClosure]:
        """Verify shortlisted candidates and return the closures that survive.

        ``max_closures`` caps the work: verification runs the full matcher and essential-matrix
        estimate per candidate, and on a long walkthrough the shortlist can be large. Candidates
        are tried in appearance order, so the cap drops the least promising.
        """
        if len(features) != len(hashes):
            raise ValueError(
                f"features and hashes must be parallel, got {len(features)} and {len(hashes)}"
            )

        closures: list[LoopClosure] = []
        for i, j, distance in self.candidates(hashes):
            if len(closures) >= max_closures:
                break

            matches = self.matcher.match(features[i], features[j])
            if matches.inlier_count < self.min_inliers:
                continue

            width, height = features[i].image_width, features[i].image_height
            motion = self.motion_estimator.estimate(matches, width, height)

            if motion.is_reliable and motion.confidence > 0.0:
                # The translation is a unit direction; scale comes from the odometry chain, so a
                # closure constrains bearing and heading, not distance.
                closures.append(
                    LoopClosure(
                        from_index=i,
                        to_index=j,
                        dx=float(motion.translation[0]),
                        dy=float(motion.translation[2]),
                        dtheta_deg=motion.yaw_deg,
                        inlier_count=matches.inlier_count,
                        hash_distance=distance,
                        confidence=motion.confidence,
                    )
                )
                continue

            colocated = self._as_colocated(matches, width, height, distance, i, j)
            if colocated is not None:
                closures.append(colocated)
        return closures

    def _as_colocated(
        self,
        matches: MatchResult,
        width: int,
        height: int,
        hash_distance: int,
        i: int,
        j: int,
    ) -> LoopClosure | None:
        """Read a zero-parallax pair as "the operator stood here again", or refuse it.

        The motion estimator rejected this pair, and for the right reason: with no translation
        the essential matrix is degenerate and no direction of travel exists to recover. But the
        *reason* matters. Sub-threshold parallax across a large, geometrically consistent set of
        correspondences is not a failure to measure motion -- it is a measurement that there was
        none, which is exactly what a return to the same spot looks like.

        Rejecting it anyway is what left loop closure finding nothing on a walkthrough that
        circled a floor and came back to its start: 31 frames, frames 0 and 30 identical to a
        perceptual hash distance of zero, and not one closure accepted.

        The bar is deliberately higher than for a bearing closure. Co-location asserts position
        directly, which is a far stronger claim than a direction, so it demands more inliers and
        parallax genuinely near zero rather than merely small -- a few degrees of parallax means
        *nearby*, and asserting an exact revisit from that would inject a metre of error as fact.
        """
        if matches.inlier_count < self.min_inliers * COLOCATED_INLIER_MULTIPLIER:
            return None

        # Correspondences must come from around the whole view, not one patch of it. Zero
        # parallax across a tight cluster says nothing: a blob of points on a blank wall agrees
        # with itself under any pose, and a degenerate match set (every point at one pixel)
        # produces a perfect zero-parallax fit that means nothing at all.
        if not _is_well_spread(matches.points_a, width, height):
            return None

        bearings_a = pixels_to_bearings(matches.points_a, width, height)
        bearings_b = pixels_to_bearings(matches.points_b, width, height)
        rotation, parallax_deg = _rotation_only_fit(bearings_a, bearings_b)
        if rotation is None or parallax_deg > MAX_COLOCATED_PARALLAX_DEG:
            return None

        # Confidence from how little parallax there is and how much agreed: both are evidence of
        # the same thing, and neither alone is enough.
        support = float(np.clip(matches.inlier_count / (SATURATION_LOOP_INLIERS), 0.0, 1.0))
        stillness = float(np.clip(1.0 - parallax_deg / MAX_COLOCATED_PARALLAX_DEG, 0.0, 1.0))
        confidence = float(np.sqrt(support * stillness))
        if confidence <= 0.0:
            return None

        return LoopClosure(
            from_index=i,
            to_index=j,
            dx=0.0,
            dy=0.0,
            dtheta_deg=_yaw_from_rotation(rotation),
            inlier_count=matches.inlier_count,
            hash_distance=hash_distance,
            confidence=confidence,
            is_colocated=True,
        )


def _is_well_spread(points: np.ndarray, width: int, height: int) -> bool:
    """Do these correspondences cover enough of the frame to support a claim about pose?

    A co-located verdict rests on parallax being near zero *everywhere*. Measured across a small
    cluster that is not a finding, and across a degenerate set it is arithmetic on nothing.
    """
    if points.ndim != 2 or points.shape[0] < 3:
        return False
    span_x = float(np.ptp(points[:, 0])) / max(width, 1)
    span_y = float(np.ptp(points[:, 1])) / max(height, 1)
    return span_x >= MIN_COLOCATED_SPREAD_X and span_y >= MIN_COLOCATED_SPREAD_Y


# ---- back end: reconciling them ---------------------------------------------
class PoseGraph:
    """SE(2) pose graph. Node 0 is fixed; every other node is free."""

    def __init__(self, initial: np.ndarray) -> None:
        if initial.ndim != 2 or initial.shape[1] != 3:
            raise ValueError(f"initial poses must be (N, 3), got {initial.shape}")
        self.poses = np.array(initial, dtype=float)
        #: The unoptimised track, kept so an outlier-rejection restart begins from a clean state
        #: rather than from a configuration the outliers already distorted.
        self._initial = np.array(initial, dtype=float)
        self.edges: list[PoseGraphEdge] = []

    @property
    def size(self) -> int:
        return int(self.poses.shape[0])

    def add_edge(self, edge: PoseGraphEdge) -> None:
        if not (0 <= edge.i < self.size and 0 <= edge.j < self.size):
            raise ValueError(f"edge ({edge.i}, {edge.j}) outside graph of {self.size} poses")
        self.edges.append(edge)

    def total_error(self) -> float:
        """Sum of information-weighted squared residuals over all edges."""
        total = 0.0
        for edge in self.edges:
            residual = self._residual(edge)
            information = self._information(edge)
            total += float(residual @ information @ residual)
        return float(total)

    def _information(self, edge: PoseGraphEdge) -> np.ndarray:
        """Diagonal information matrix, sized to match the edge's residual.

        Loop edges are down-weighted by a Huber factor once their residual exceeds
        :data:`HUBER_DELTA_RADIANS`. Plain least squares weights a constraint by the *square* of
        how wrong it is, so a single false closure outvotes the entire odometry chain -- which is
        exactly backwards, and exactly what was measured before this existed.

        Odometry edges are not robustified: they are the reference the closures are judged
        against, and down-weighting them would let a confident outlier reshape the track.
        """
        if not edge.is_loop:
            return np.diag([edge.info_xy, edge.info_xy, edge.info_theta])

        weight = self._huber_weight(edge)
        if edge.is_colocated:
            return np.diag([edge.info_xy * weight, edge.info_xy * weight, edge.info_theta * weight])
        return np.diag([edge.info_xy * weight, edge.info_theta * weight])

    def _huber_weight(self, edge: PoseGraphEdge) -> float:
        """1 inside the Huber radius, falling off as delta/|r| outside it."""
        magnitude = float(np.linalg.norm(self._raw_residual(edge)))
        if magnitude <= HUBER_DELTA_RADIANS:
            return 1.0
        return HUBER_DELTA_RADIANS / magnitude

    def _raw_residual(self, edge: PoseGraphEdge) -> np.ndarray:
        """Alias for :meth:`_residual`, named at the call sites that must not be weighted."""
        return self._residual(edge)

    def _residual(self, edge: PoseGraphEdge) -> np.ndarray:
        """Constraint violation. Three components for odometry, two for a loop closure.

        Odometry knows how far it travelled -- the distance comes from the pace assumption, not
        from the imagery -- so its residual is a full relative pose.

        A loop closure does not. Two-view geometry recovers the *direction* to the other camera
        and the heading change, never the distance: a single moving camera cannot see scale. So a
        closure constrains bearing and heading only, and its residual is
        ``(bearing_error, heading_error)``.

        Treating the closure's unit translation as a metric displacement instead is not a small
        approximation, it is a fabricated measurement. It showed up immediately: a *zero-drift*
        square with an exact closure scored an initial error of 762 rather than 0, and the solver
        obediently pulled the loop's endpoints to exactly 1.0 units apart -- closing the gap for
        entirely the wrong reason and reporting success while doing it.
        """
        pose_i, pose_j = self.poses[edge.i], self.poses[edge.j]
        predicted = _relative(pose_i, pose_j)

        if not edge.is_loop or edge.is_colocated:
            return np.array(
                [
                    predicted[0] - edge.dx,
                    predicted[1] - edge.dy,
                    _wrap(predicted[2] - edge.dtheta),
                ]
            )

        observed_bearing = math.atan2(edge.dy, edge.dx)
        distance = math.hypot(predicted[0], predicted[1])
        if distance < _MIN_BEARING_DISTANCE:
            # Two poses on top of each other have no meaningful bearing between them. Report no
            # bearing error rather than a direction read off floating-point dust.
            return np.array([0.0, _wrap(predicted[2] - edge.dtheta)])

        predicted_bearing = math.atan2(predicted[1], predicted[0])
        return np.array(
            [
                _wrap(predicted_bearing - observed_bearing),
                _wrap(predicted[2] - edge.dtheta),
            ]
        )

    def optimise(
        self,
        *,
        max_iterations: int = MAX_ITERATIONS,
        tolerance: float = CONVERGENCE_DELTA,
        reject_outliers: bool = True,
    ) -> OptimisationReport:
        """Solve the graph, discard loop closures that turn out to be false, solve again.

        Two passes, because a robust kernel limits an outlier's influence but does not remove it.
        The first solve is robust enough to find a sane configuration; any loop edge still
        disagreeing by more than :data:`RESIDUAL_OUTLIER_RADIANS` in that configuration is a
        false positive, and the second solve is run without it.

        Measured need, not theory. Before this existed, a rendered rectangular circuit produced
        twelve "verified" closures, two of them between different corners of the room, and the
        optimised track was 24% *worse* than plain dead reckoning -- while reporting convergence,
        a 97.9% drop in constraint error and high confidence. Every internal signal said success.

        Rejections are counted in the report rather than silently dropped: a walkthrough where
        half the closures were discarded is one to be suspicious of, and only the caller can
        judge that.
        """
        first = self._solve_once(max_iterations=max_iterations, tolerance=tolerance)
        if not reject_outliers:
            return first

        loop_edges = [edge for edge in self.edges if edge.is_loop]
        if not loop_edges:
            return first

        outliers = [
            edge
            for edge in loop_edges
            if float(np.linalg.norm(self._raw_residual(edge))) > RESIDUAL_OUTLIER_RADIANS
        ]
        if not outliers:
            return first

        kept = [edge for edge in self.edges if edge not in outliers]
        if not any(edge.is_loop for edge in kept):
            # Every closure was an outlier, so there is nothing to reconcile. Returning the
            # odometry untouched is the honest answer, not a track bent by constraints that were
            # all wrong.
            self.poses = self._initial.copy()
            self.edges = kept
            report = self._solve_once(max_iterations=max_iterations, tolerance=tolerance)
            report.loop_closures_rejected = len(outliers)
            report.notes.append(
                f"all {len(outliers)} candidate closure(s) were rejected as inconsistent - "
                "the track is unchanged dead reckoning"
            )
            return report

        # Restart from the unoptimised track: the first solve was distorted by the outliers, and
        # using it as the starting point would carry that distortion into the second.
        self.poses = self._initial.copy()
        self.edges = kept
        second = self._solve_once(max_iterations=max_iterations, tolerance=tolerance)
        second.loop_closures_rejected = len(outliers)
        second.notes.append(
            f"rejected {len(outliers)} closure(s) that still disagreed by more than "
            f"{np.degrees(RESIDUAL_OUTLIER_RADIANS):.0f}deg after the robust solve"
        )
        return second

    def _solve_once(
        self, *, max_iterations: int = MAX_ITERATIONS, tolerance: float = CONVERGENCE_DELTA
    ) -> OptimisationReport:
        """One Levenberg-Marquardt solve, holding node 0 at the origin.

        Node 0 is fixed because a pose graph built from relative constraints has a free global
        rigid transform: without an anchor the normal equations are singular and the solution
        drifts as a whole. Fixing the first pose also keeps the output in the same frame the
        unoptimised track used, so the similarity fit that anchors to a floor plan is unaffected.
        """
        notes: list[str] = []
        initial_error = self.total_error()

        loops = sum(1 for edge in self.edges if edge.is_loop)
        if not self.edges:
            notes.append("no constraints - nothing to optimise")
            return OptimisationReport(
                converged=True,
                iterations=0,
                initial_error=0.0,
                final_error=0.0,
                max_correction=0.0,
                loop_closures_used=0,
                notes=notes,
            )
        if loops == 0:
            # Optimising an open chain is a no-op: the odometry already satisfies every edge
            # exactly. Saying so is more useful than reporting a converged solve that did nothing.
            notes.append("no loop closures - the odometry chain is already the optimum")
            return OptimisationReport(
                converged=True,
                iterations=0,
                initial_error=initial_error,
                final_error=initial_error,
                max_correction=0.0,
                loop_closures_used=0,
                notes=notes,
            )

        before = self.poses.copy()
        free = self.size - 1  # node 0 is fixed
        converged = False
        iterations = 0
        current_error = initial_error
        damping = INITIAL_DAMPING

        for attempt in range(1, max_iterations + 1):
            iterations = attempt
            hessian, gradient = self._normal_equations(free)

            # Levenberg-Marquardt rather than plain Gauss-Newton. Measured need, not caution: an
            # undamped solve on a 24-pose square with 1.5 deg/step of yaw bias oscillated for 60
            # iterations without settling, because a 34 deg accumulated heading error puts the
            # initial guess well outside the region where the linearisation holds. Damping
            # interpolates toward gradient descent until the step is trustworthy.
            step, trial_error = None, current_error
            while damping <= MAX_DAMPING:
                candidate = self._solve(hessian, gradient, damping, free)
                if candidate is None:
                    damping *= DAMPING_FACTOR
                    continue

                trial = self.poses.copy()
                trial[1:] += candidate.reshape(free, 3)
                trial[1:, 2] = np.array([_wrap(angle) for angle in trial[1:, 2]])

                error_at_trial = self._error_at(trial)
                if error_at_trial < current_error:
                    step, trial_error = candidate, error_at_trial
                    self.poses = trial
                    damping = max(damping / DAMPING_FACTOR, MIN_DAMPING)
                    break

                # The step made things worse, so trust the linearisation less and try again.
                damping *= DAMPING_FACTOR

            if step is None:
                # No damping value produced an improvement: this is the optimum reachable from
                # here. Converged in the only sense that matters -- further iteration is futile.
                converged = True
                break

            current_error = trial_error
            if float(np.max(np.abs(step))) < tolerance:
                converged = True
                break

        final_error = self.total_error()
        if final_error > initial_error:
            # Unreachable while every accepted step strictly decreases the error, and kept as a
            # guarantee rather than a hope: a track worse than its input must never be published.
            notes.append(
                f"optimisation increased the error ({initial_error:.4f} -> {final_error:.4f}) - "
                "keeping the unoptimised track"
            )
            self.poses = before
            return OptimisationReport(
                converged=False,
                iterations=iterations,
                initial_error=initial_error,
                final_error=initial_error,
                max_correction=0.0,
                loop_closures_used=loops,
                notes=notes,
            )

        if not converged:
            notes.append(f"stopped at the {max_iterations}-iteration limit without converging")

        corrections = np.linalg.norm(self.poses[:, :2] - before[:, :2], axis=1)
        steps = np.linalg.norm(np.diff(before[:, :2], axis=0), axis=1)
        median_step = float(np.median(steps)) if steps.size else 0.0
        return OptimisationReport(
            converged=converged,
            iterations=iterations,
            initial_error=initial_error,
            final_error=final_error,
            max_correction=float(corrections.max()),
            max_correction_steps=(
                float(corrections.max() / median_step) if median_step > 1e-9 else 0.0
            ),
            loop_closures_used=loops,
            notes=notes,
        )

    def _normal_equations(self, free: int) -> tuple[np.ndarray, np.ndarray]:
        """Accumulate Jᵀ·Ω·J and Jᵀ·Ω·r over every edge, skipping the fixed node 0."""
        hessian = np.zeros((free * 3, free * 3))
        gradient = np.zeros(free * 3)

        for edge in self.edges:
            residual = self._residual(edge)
            jac_i, jac_j = self._jacobians(edge)
            information = self._information(edge)

            for node, jacobian in ((edge.i, jac_i), (edge.j, jac_j)):
                if node == 0:
                    continue
                slot = slice((node - 1) * 3, (node - 1) * 3 + 3)
                gradient[slot] += jacobian.T @ information @ residual
                hessian[slot, slot] += jacobian.T @ information @ jacobian

            if edge.i != 0 and edge.j != 0:
                slot_i = slice((edge.i - 1) * 3, (edge.i - 1) * 3 + 3)
                slot_j = slice((edge.j - 1) * 3, (edge.j - 1) * 3 + 3)
                cross = jac_i.T @ information @ jac_j
                hessian[slot_i, slot_j] += cross
                hessian[slot_j, slot_i] += cross.T

        return hessian, gradient

    def _solve(
        self, hessian: np.ndarray, gradient: np.ndarray, damping: float, free: int
    ) -> np.ndarray | None:
        """One damped step, or None when the damped system is still not solvable.

        Damping scales the diagonal rather than adding a constant, so it is invariant to the
        units of each block: translation is in track units and heading in radians, and a constant
        added to both would damp one far harder than the other.
        """
        scaled = hessian + np.diag(np.diag(hessian) * damping) + np.eye(free * 3) * 1e-12
        try:
            return np.linalg.solve(scaled, -gradient)
        except np.linalg.LinAlgError:
            return None

    def _error_at(self, poses: np.ndarray) -> float:
        """Total error for a candidate pose set, without committing to it."""
        original = self.poses
        self.poses = poses
        try:
            return self.total_error()
        finally:
            self.poses = original

    def _jacobians(self, edge: PoseGraphEdge) -> tuple[np.ndarray, np.ndarray]:
        """Analytic derivatives of the edge's residual w.r.t. poses i and j.

        Shapes follow :meth:`_residual`: (3, 3) for odometry, (2, 3) for a bearing-only closure.
        Verified against central finite differences in ``test_slam.py``, because a wrong Jacobian
        does not fail loudly -- it converges slowly to somewhere plausible.
        """
        pose_i, pose_j = self.poses[edge.i], self.poses[edge.j]
        theta = pose_i[2]
        c, s = math.cos(theta), math.sin(theta)
        dx, dy = pose_j[0] - pose_i[0], pose_j[1] - pose_i[1]

        # Derivatives of the relative *position* p = R(-theta_i)(p_j - p_i), and of the heading.
        position_i = np.array(
            [
                [-c, -s, -s * dx + c * dy],
                [s, -c, -c * dx - s * dy],
            ]
        )
        position_j = np.array([[c, s, 0.0], [-s, c, 0.0]])
        heading_i = np.array([0.0, 0.0, -1.0])
        heading_j = np.array([0.0, 0.0, 1.0])

        if not edge.is_loop or edge.is_colocated:
            return (
                np.vstack([position_i, heading_i]),
                np.vstack([position_j, heading_j]),
            )

        # Bearing chain rule: d/dx atan2(p_y, p_x) = (-p_y, p_x) / |p|^2, applied to the position
        # derivatives above.
        predicted = _relative(pose_i, pose_j)
        squared = predicted[0] ** 2 + predicted[1] ** 2
        if squared < _MIN_BEARING_DISTANCE**2:
            # Matches the residual's degenerate branch: no bearing information, so no gradient.
            bearing_i = np.zeros(3)
            bearing_j = np.zeros(3)
        else:
            gradient = np.array([-predicted[1], predicted[0]]) / squared
            bearing_i = gradient @ position_i
            bearing_j = gradient @ position_j

        return (
            np.vstack([bearing_i, heading_i]),
            np.vstack([bearing_j, heading_j]),
        )


def build_graph(
    poses: list[RelativePose],
    motions: list[RelativeMotion],
    closures: list[LoopClosure],
) -> PoseGraph:
    """Assemble a pose graph from a dead-reckoned track plus verified closures.

    The odometry edges are taken from the *initial track* rather than recomputed from the motions,
    so the graph's starting point satisfies them exactly. The loop edges are the only source of
    disagreement, which is what the optimiser then distributes around the loop.
    """
    initial = np.array([[pose.x, pose.y, math.radians(pose.heading_deg)] for pose in poses])
    graph = PoseGraph(initial)

    for index in range(len(poses) - 1):
        relative = _relative(initial[index], initial[index + 1])
        # Weight each step by its own confidence: a shaky step should yield to a confident one
        # when the loop is closed, rather than every step absorbing an equal share of the error.
        confidence = 1.0
        if index < len(motions):
            confidence = max(motions[index].confidence, 0.05)
        graph.add_edge(
            PoseGraphEdge(
                i=index,
                j=index + 1,
                dx=float(relative[0]),
                dy=float(relative[1]),
                dtheta=float(relative[2]),
                info_xy=confidence,
                info_theta=confidence,
                is_loop=False,
            )
        )

    for closure in closures:
        weight = LOOP_INFORMATION_SCALE * max(closure.confidence, 0.05)
        graph.add_edge(
            PoseGraphEdge(
                i=closure.from_index,
                j=closure.to_index,
                dx=closure.dx,
                dy=closure.dy,
                dtheta=math.radians(closure.dtheta_deg),
                is_colocated=closure.is_colocated,
                # For a loop edge `info_xy` weights the *bearing* residual (see `_residual`).
                # Bearing and heading are both angles observed by the same estimate, so they
                # carry the same weight; there is no fabricated distance left to discount.
                info_xy=weight,
                info_theta=weight,
                is_loop=True,
            )
        )

    return graph


@dataclass(slots=True)
class SlamOutcome:
    """A track after loop closure, with everything needed to judge it."""

    poses: list[RelativePose]
    closures: list[LoopClosure]
    report: OptimisationReport
    confidence: MappingConfidence
    #: True when the track was left as dead reckoning produced it.
    unchanged: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "method": "dead_reckoning" if self.unchanged else "pose_graph_slam",
            "loop_closures": [closure.to_dict() for closure in self.closures],
            "optimisation": self.report.to_dict(),
            "confidence": self.confidence.value,
        }


class GraphSlamPositionEstimator:
    """Dead reckoning, then loop closure, then pose-graph optimisation.

    Satisfies the same :class:`app.vision.position.PositionEstimator` contract as dead reckoning,
    so it is a drop-in replacement; :meth:`refine` is the additional entry point that needs the
    imagery-derived hashes and features the plain protocol does not carry.
    """

    def __init__(self, base_estimator, detector: LoopClosureDetector) -> None:
        self.base = base_estimator
        self.detector = detector

    def integrate(self, motions, distances) -> list[RelativePose]:
        """Plain odometry. Without imagery there are no revisits to find."""
        return self.base.integrate(motions, distances)

    def refine(
        self,
        poses: list[RelativePose],
        motions: list[RelativeMotion],
        features: list[FeatureSet],
        hashes: list[str],
        *,
        base_confidence: MappingConfidence = MappingConfidence.MEDIUM,
    ) -> SlamOutcome:
        """Close loops in an existing track and return the reconciled version.

        On any refusal the *original* track is returned with ``unchanged=True``. A caller that
        wants to know whether SLAM actually helped reads that flag and the report, rather than
        having to compare tracks itself.
        """
        if len(poses) < MIN_LOOP_SEPARATION + 2:
            return SlamOutcome(
                poses,
                [],
                OptimisationReport(
                    converged=True,
                    iterations=0,
                    initial_error=0.0,
                    final_error=0.0,
                    max_correction=0.0,
                    loop_closures_used=0,
                    notes=[f"only {len(poses)} poses - too short to loop"],
                ),
                base_confidence,
                unchanged=True,
            )

        closures = self.detector.detect(features, hashes)
        if not closures:
            return SlamOutcome(
                poses,
                [],
                OptimisationReport(
                    converged=True,
                    iterations=0,
                    initial_error=0.0,
                    final_error=0.0,
                    max_correction=0.0,
                    loop_closures_used=0,
                    notes=["no verified revisits - the path did not close"],
                ),
                base_confidence,
                unchanged=True,
            )

        graph = build_graph(poses, motions, closures)
        report = graph.optimise()

        if not report.converged or report.max_correction <= 0.0:
            return SlamOutcome(poses, closures, report, base_confidence, unchanged=True)

        refined = [
            RelativePose(
                frame_index=poses[index].frame_index,
                x=float(graph.poses[index, 0]),
                y=float(graph.poses[index, 1]),
                heading_deg=float(math.degrees(graph.poses[index, 2])),
                step_confidence=poses[index].step_confidence,
                # A closed loop bounds drift instead of compounding it, so the late-frame penalty
                # that dead reckoning applies no longer describes this track. Raised, never to
                # certainty, and only for frames the closures actually constrain.
                cumulative_confidence=_closed_loop_confidence(poses, index, closures),
            )
            for index in range(len(poses))
        ]

        return SlamOutcome(refined, closures, report, base_confidence, unchanged=False)


def _closed_loop_confidence(
    poses: list[RelativePose], index: int, closures: list[LoopClosure]
) -> float:
    """Cumulative confidence for a pose inside a closed loop.

    Dead reckoning multiplies step confidences, so frame 40 of a good walkthrough can read 0.2
    purely from chain length. Once a closure ties frame 40 back to frame 1 that pessimism is
    wrong -- but so is discarding it, because a pose *outside* any closure is still only as good
    as the chain that reached it. So the geometric-mean chain is kept and lifted toward the local
    step confidence only for poses that a closure actually spans.
    """
    pose = poses[index]
    spanned = any(
        closure.from_index <= index <= closure.to_index and closure.confidence > 0.0
        for closure in closures
    )
    if not spanned:
        return pose.cumulative_confidence
    best = max(
        (
            closure.confidence
            for closure in closures
            if closure.from_index <= index <= closure.to_index
        ),
        default=0.0,
    )
    # Halfway between the chained value and what the step alone would support, weighted by how
    # strong the spanning closure is.
    lifted = pose.cumulative_confidence + best * (pose.step_confidence - pose.cumulative_confidence)
    return float(min(max(lifted, pose.cumulative_confidence), pose.step_confidence))
