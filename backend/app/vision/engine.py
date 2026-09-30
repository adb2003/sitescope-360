"""VisionEngine — the single façade the rest of the platform talks to (§15, §31).

```
VisionEngine
 ├── FeatureDetector    OrbFeatureDetector
 ├── FeatureMatcher     BruteForceMatcher   (ratio test + spherical RANSAC)
 ├── MotionEstimator    EssentialMotionEstimator
 ├── PositionEstimator  DeadReckoningPositionEstimator
 ├── SlamEstimator     GraphSlamPositionEstimator  (loop closure + SE(2) pose graph)
 ├── KeyframeSelector
 └── MapBuilder
```

Two entry points for tracks, and the difference is deliberate. :meth:`estimate_track` is plain
odometry. :meth:`estimate_track_with_slam` additionally looks for revisits and reconciles them,
returning *both* results so a caller can say which one it is showing.

Every component is injected, so replacing the estimator with ORB-SLAM3, OpenVSLAM, COLMAP or
OpenSfM is a constructor change — the pipeline stages call only the methods on this class.

The engine deliberately exposes no "detect progress" or "detect defects" method. Those belong to
Phase 3 and are declared as unimplemented interfaces in :mod:`app.vision.ai` rather than faked.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from app.core.enums import ProjectionType
from app.vision.features import (
    BruteForceMatcher,
    FeatureMatcher,
    OrbFeatureDetector,
)
from app.vision.mapbuilder import MapBuilder
from app.vision.motion import EssentialMotionEstimator
from app.vision.panorama import describe_image, dhash, hamming_distance
from app.vision.position import (
    DeadReckoningPositionEstimator,
    KeyframeSelector,
    summarize_mapping,
)
from app.vision.quality import ImageQualityAnalyzer
from app.vision.slam import (
    GraphSlamPositionEstimator,
    LoopClosureDetector,
    OptimisationReport,
    SlamOutcome,
)
from app.vision.types import (
    FeatureSet,
    ImageInfo,
    MappingOutcome,
    MatchResult,
    QualityMetrics,
    RelativeMotion,
)


class VisionEngine:
    def __init__(
        self,
        *,
        detector: OrbFeatureDetector | None = None,
        matcher: FeatureMatcher | None = None,
        motion_estimator: EssentialMotionEstimator | None = None,
        position_estimator: DeadReckoningPositionEstimator | None = None,
        keyframe_selector: KeyframeSelector | None = None,
        map_builder: MapBuilder | None = None,
        quality_analyzer: ImageQualityAnalyzer | None = None,
        slam_estimator: GraphSlamPositionEstimator | None = None,
    ) -> None:
        self.detector = detector or OrbFeatureDetector()
        self.matcher = matcher or BruteForceMatcher()
        self.motion_estimator = motion_estimator or EssentialMotionEstimator()
        self.position_estimator = position_estimator or DeadReckoningPositionEstimator()
        self.keyframe_selector = keyframe_selector or KeyframeSelector()
        self.map_builder = map_builder or MapBuilder()
        self.quality_analyzer = quality_analyzer or ImageQualityAnalyzer()
        # Shares the matcher and motion estimator: loop closure verifies candidates with
        # exactly the geometry the odometry uses, so a closure can never be accepted on
        # evidence the rest of the pipeline would reject.
        self.slam_estimator = slam_estimator or GraphSlamPositionEstimator(
            self.position_estimator,
            LoopClosureDetector(self.matcher, self.motion_estimator),
        )

    # ---- single-frame analysis -----------------------------------------
    def describe(self, image: np.ndarray, *, xmp_projection: str | None = None) -> ImageInfo:
        return describe_image(image, xmp_projection=xmp_projection)

    def assess_quality(
        self, image: np.ndarray, *, projection: ProjectionType = ProjectionType.EQUIRECTANGULAR
    ) -> QualityMetrics:
        return self.quality_analyzer.analyze(image, projection=projection)

    def perceptual_hash(self, image: np.ndarray) -> str:
        return dhash(image)

    def detect_features(
        self, image: np.ndarray, *, projection: ProjectionType = ProjectionType.EQUIRECTANGULAR
    ) -> FeatureSet:
        return self.detector.detect(image, projection)

    # ---- pairwise analysis ---------------------------------------------
    def match(self, a: FeatureSet, b: FeatureSet) -> MatchResult:
        return self.matcher.match(a, b)

    def estimate_motion(self, a: FeatureSet, b: FeatureSet) -> RelativeMotion:
        matches = self.matcher.match(a, b)
        return self.motion_estimator.estimate(matches, a.image_width, a.image_height)

    def similarity(self, a: FeatureSet, b: FeatureSet) -> float:
        """0–1 visual similarity from verified correspondences.

        Used to suggest which frame from an earlier session corresponds to a frame in a new one,
        so the compare view can pair up dates even before locations are assigned.
        """
        matches = self.matcher.match(a, b)
        denominator = max(min(a.count, b.count), 1)
        return float(np.clip(matches.inlier_count / denominator, 0.0, 1.0))

    def hash_distance(self, hash_a: str, hash_b: str) -> int:
        return hamming_distance(hash_a, hash_b)

    # ---- sequence analysis ---------------------------------------------
    def select_keyframes(
        self, hashes: list[str | None], quality_scores: list[float | None]
    ) -> list[bool]:
        return self.keyframe_selector.select(hashes, quality_scores)

    def estimate_track(
        self,
        features: list[FeatureSet],
        intervals: list[float],
    ) -> MappingOutcome:
        """Estimate a relative track from an ordered list of frame feature sets.

        ``intervals[i]`` is the elapsed seconds between ``features[i]`` and ``features[i + 1]``.
        """
        if len(features) < 2:
            return summarize_mapping([], [])

        motions: list[RelativeMotion] = []
        for index in range(len(features) - 1):
            motions.append(
                self.motion_estimator.estimate(
                    self.matcher.match(features[index], features[index + 1]),
                    features[index].image_width,
                    features[index].image_height,
                )
            )

        poses = self.position_estimator.integrate(motions, intervals)
        return summarize_mapping(poses, motions)

    def estimate_track_with_slam(
        self,
        features: list[FeatureSet],
        intervals: list[float],
        hashes: list[str],
    ) -> tuple[MappingOutcome, SlamOutcome]:
        """Estimate a track, then close any loops in it (§15).

        Returns both outcomes rather than only the refined one, because the caller needs to know
        what changed and why. The odometry outcome carries the published confidence and the §46
        refusal decision; the SLAM outcome says which revisits were verified and how far the
        track moved. A consumer that shows a track must be able to say which of the two produced
        it, so :meth:`SlamOutcome.to_dict` names the method explicitly.

        ``hashes[i]`` is the perceptual hash of the frame behind ``features[i]``. They come from
        the pipeline, which already computes them for near-duplicate rejection, so loop closure
        adds no hashing work.
        """
        if len(features) < 2:
            empty = summarize_mapping([], [])
            return empty, SlamOutcome(
                [],
                [],
                OptimisationReport(
                    converged=True,
                    iterations=0,
                    initial_error=0.0,
                    final_error=0.0,
                    max_correction=0.0,
                    loop_closures_used=0,
                    notes=["no frames"],
                ),
                empty.confidence,
                unchanged=True,
            )

        motions: list[RelativeMotion] = []
        for index in range(len(features) - 1):
            motions.append(
                self.motion_estimator.estimate(
                    self.matcher.match(features[index], features[index + 1]),
                    features[index].image_width,
                    features[index].image_height,
                )
            )

        poses = self.position_estimator.integrate(motions, intervals)
        outcome = summarize_mapping(poses, motions)

        # Closing loops on a track the platform has already refused would be wasted work, and
        # publishing a "refined" version of a track nobody will show would be misleading.
        if outcome.requires_manual_placement:
            return outcome, SlamOutcome(
                poses,
                [],
                OptimisationReport(
                    converged=True,
                    iterations=0,
                    initial_error=0.0,
                    final_error=0.0,
                    max_correction=0.0,
                    loop_closures_used=0,
                    notes=["odometry confidence too low to publish - loop closure not attempted"],
                ),
                outcome.confidence,
                unchanged=True,
            )

        slam = self.slam_estimator.refine(
            poses, motions, features, hashes, base_confidence=outcome.confidence
        )
        if slam.unchanged:
            return outcome, slam

        refined = MappingOutcome(
            poses=slam.poses,
            confidence=outcome.confidence,
            confidence_score=outcome.confidence_score,
            note=(
                # `loop_closures_used`, not `len(slam.closures)`: the latter counts candidates
                # including ones the optimiser then rejected as false, and reporting those as
                # "reconciled" overstated the evidence behind the track.
                f"{outcome.note} Loop closure reconciled "
                f"{slam.report.loop_closures_used} verified revisit(s)"
                + (
                    f" and rejected {slam.report.loop_closures_rejected} as inconsistent"
                    if slam.report.loop_closures_rejected
                    else ""
                )
                + f"; the largest pose correction was "
                f"{slam.report.max_correction_steps:.1f}x the median step."
            ),
            requires_manual_placement=outcome.requires_manual_placement,
            # Carried over rather than left to default. These describe the frame *pairs* — how
            # many features matched and survived verification between each consecutive frame —
            # and loop closure does not change any of that; it moves the poses the pairs were
            # integrated into. Dropping them here silently emptied the stage's diagnostics in
            # exactly the case where the track is most worth inspecting, and took the evidence
            # walkthrough detection reads with it.
            per_pair=outcome.per_pair,
        )
        return refined, slam

    def describe_capabilities(self) -> dict[str, Any]:
        """Machine-readable statement of what is real, so the UI never overclaims (§46)."""
        return {
            "image_quality": {
                "implemented": True,
                "method": (
                    "classical CV (Laplacian variance, histogram statistics, seam continuity)"
                ),
                "provenance": "measured",
            },
            "feature_matching": {
                "implemented": True,
                "method": "ORB + Lowe ratio test + spherical essential-matrix RANSAC",
                "provenance": "measured",
            },
            "image_similarity": {
                "implemented": True,
                "method": "verified inlier ratio",
                "provenance": "measured",
            },
            "relative_positioning": {
                "implemented": True,
                "method": "two-view spherical essential matrix, dead-reckoned",
                "provenance": "estimated",
                "limitations": [
                    "no metric scale — direction only",
                    "assumes planar motion on one floor",
                    "drifts over long sequences unless a loop is closed",
                ],
            },
            "loop_closure": {
                "implemented": True,
                "method": (
                    "perceptual-hash shortlist, geometric verification, short-range constraints "
                    "between frames 2-3 apart, SE(2) pose-graph optimisation "
                    "(Levenberg-Marquardt)"
                ),
                "provenance": "estimated",
                "limitations": [
                    # Was "only helps a walkthrough that revisits a place it has already been"
                    # until short-range constraints were added. Measured over six rendered
                    # walks: median 33% closer to ground truth than raw odometry, better on four
                    # of six, and on the two walks with no revisit at all the improvement came
                    # entirely from the short-range edges.
                    "helps most when the walkthrough revisits somewhere; short-range constraints "
                    "give a smaller gain on a walk that never doubles back",
                    "one of six measured walks came out worse than plain odometry — a false "
                    "closure the robust kernel reduced but did not remove",
                    "closures constrain bearing and heading, never distance",
                    "no landmark map and no bundle adjustment over 3D points",
                    "single session only — no relocalisation against an earlier capture",
                    # Measured, not hypothetical: repetitive structure produced false closures
                    # that made a rendered circuit 24% worse than plain odometry before the
                    # robust kernel and residual rejection were added. Those catch closures that
                    # contradict each other; a single unchallenged one can still distort a track.
                    "a lone false closure the graph can absorb is not detectable",
                    # Both numbers measured on rendered circuits with known ground truth. They
                    # point in opposite directions and both are reported, because a single
                    # figure here would be a sales claim rather than a measurement.
                    "helps when there is drift to remove (38% better on a drifted circuit) and "
                    "can add a little error when there is not (37 mm worse on an already "
                    "accurate one) — it bounds drift rather than always reducing error",
                ],
            },
            "cross_session_localisation": {
                "implemented": True,
                "method": (
                    "perceptual-hash shortlist against placed frames of earlier captures of the "
                    "same floor, ORB matching, spherical epipolar verification, rotation-only "
                    "fit; the least-parallax candidate anchors the frame, and three anchors fit "
                    "a similarity transform over the whole track"
                ),
                "provenance": "estimated",
                "limitations": [
                    # The number that decides whether this feature exists at all, and the one it
                    # is most likely to be wrong about. It is stated here rather than buried
                    # because everything the feature produces inherits it.
                    "a match means the two cameras were close, not identical: the 8 degree "
                    "parallax bar is roughly half a metre at 3 m depth and roughly 1.7 m across "
                    "a 12 m space, because parallax is an angle and monocular vision has no "
                    "scale",
                    "that tolerance can exceed the spacing between capture points, so the "
                    "nearest candidate by parallax is chosen — a wrong choice shifts the whole "
                    "capture by one point and a similarity fit reproduces it with no residual",
                    "three anchors are required before a track is placed, because a two-point "
                    "similarity fit matches its own anchors exactly and its residual is "
                    "therefore evidence of nothing",
                    "a floor that has changed beyond recognition since it was last captured "
                    "will not be recognised, which is a refusal rather than a wrong answer",
                    "points created by recognition anchor the next capture, so the tolerance "
                    "compounds along a chain; each point records how many hops it is from one a "
                    "person placed, and creating points from references more than three hops out "
                    "is refused (reusing one is always allowed, as it adds no new coordinate)",
                    "the first capture of any floor still has to be anchored by hand",
                ],
            },
            "walkthrough_grouping": {
                "implemented": True,
                "method": (
                    "per-pair evidence already computed by mapping: a gap measured against the "
                    "session's own median interval, and verified inlier counts measured against "
                    "the session's own typical link; a pair too featureless to match is reported "
                    "as unknown rather than as a break"
                ),
                # Not "estimated": nothing is written to a capture from this without a person
                # confirming it. The report is a measurement of the frames; the split is theirs.
                "provenance": "measured",
                "limitations": [
                    # The boundary of the feature, stated first because everything else assumes
                    # it. No amount of imagery will ever say which floor a walk was on.
                    "detects *that* a walk stopped, never *where* it resumed — which floor each "
                    "walkthrough was on is a fact about the building, not about the pixels, so "
                    "the split is proposed and never applied automatically",
                    "an operator who walks straight from one identical bay into another leaves "
                    "no discontinuity to find, and one who pauses without moving leaves a gap "
                    "that is not a break; the two signals correct each other but neither is "
                    "complete",
                    "two frames with too little texture to match cannot distinguish 'a different "
                    "place' from 'nothing to match'; such a pair is reported as unknown and is "
                    "only proposed as a break when the clock independently agrees",
                    "the inlier bar is a fraction of the session's own typical link, so a "
                    "capture that is poor throughout is judged against its own standard — which "
                    "is the intent, and also means a uniformly poor capture is harder to split",
                    "a break that would leave fewer than four frames on one side is reported but "
                    "not proposed",
                    "splitting re-points rows rather than copying imagery, so a walkthrough's "
                    "frames remain stored under the upload it came from",
                ],
            },
            "object_detection": {"implemented": False, "phase": 3},
            "defect_detection": {"implemented": False, "phase": 3},
            "progress_detection": {"implemented": False, "phase": 3},
            "bim_comparison": {"implemented": False, "phase": 3},
        }


_engine: VisionEngine | None = None


def get_vision_engine() -> VisionEngine:
    """Process-wide engine. ORB creation is not free, so it is built once."""
    global _engine
    if _engine is None:
        _engine = VisionEngine()
    return _engine
