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
                f"{outcome.note} Loop closure reconciled {len(slam.closures)} verified "
                f"revisit(s); the largest pose correction was "
                f"{slam.report.max_correction:.2f} track units."
            ),
            requires_manual_placement=outcome.requires_manual_placement,
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
                    "perceptual-hash shortlist, geometric verification, SE(2) pose-graph "
                    "optimisation (Levenberg-Marquardt)"
                ),
                "provenance": "estimated",
                "limitations": [
                    "only helps a walkthrough that revisits a place it has already been",
                    "closures constrain bearing and heading, never distance",
                    "no landmark map and no bundle adjustment over 3D points",
                    "single session only — no relocalisation against an earlier capture",
                    # Measured, not hypothetical: repetitive structure produced false closures
                    # that made a rendered circuit 24% worse than plain odometry before the
                    # robust kernel and residual rejection were added. Those catch closures that
                    # contradict each other; a single unchallenged one can still distort a track.
                    "a lone false closure the graph can absorb is not detectable — measured "
                    "improvement on a rendered circuit is 38% over odometry, not a guarantee",
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
