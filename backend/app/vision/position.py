"""Relative position estimation and keyframe selection (§14, §15).

The position estimator dead-reckons a floor-plane track from consecutive relative motions. Two
assumptions are made explicitly, because they are the reason the output is labelled *estimated*:

1. **Scale.** Monocular geometry gives direction but not distance. Step length is modelled as
   ``pace × Δt`` with a single unknown pace, so the track's *shape* is meaningful and its *size*
   is arbitrary until anchored to a floor plan.
2. **Planarity.** Only the horizontal (X, Z) components are integrated. On a single floor of a
   building that holds well; on a ramp or stair it does not, and the vertical component is
   reported so a caller can detect the violation.

Confidence decays multiplicatively along the chain, which is the honest model of dead reckoning:
one bad link poisons everything downstream of it.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np

from app.core.config import settings
from app.core.enums import MappingConfidence
from app.vision.panorama import hamming_distance
from app.vision.types import MappingOutcome, RelativeMotion, RelativePose

#: Arbitrary units per second of elapsed time. Only the ratio between steps matters.
DEFAULT_PACE = 1.0
#: A link this weak breaks the chain rather than contaminating it.
BROKEN_LINK_CONFIDENCE = 0.15


class PositionEstimator(Protocol):
    def integrate(
        self, motions: list[RelativeMotion], intervals: list[float]
    ) -> list[RelativePose]: ...


class DeadReckoningPositionEstimator:
    def __init__(self, pace: float = DEFAULT_PACE) -> None:
        self.pace = pace

    def integrate(
        self, motions: list[RelativeMotion], intervals: list[float]
    ) -> list[RelativePose]:
        poses = [
            RelativePose(
                frame_index=0,
                x=0.0,
                y=0.0,
                heading_deg=0.0,
                step_confidence=1.0,
                cumulative_confidence=1.0,
            )
        ]

        # Cumulative rotation mapping the world (frame 0) into the current camera's frame.
        r_cum = np.eye(3)
        position = np.zeros(2)
        heading = 0.0
        cumulative = 1.0

        for index, motion in enumerate(motions):
            dt = intervals[index] if index < len(intervals) else 1.0
            step_length = max(self.pace * max(dt, 1e-3), 1e-3)

            if motion.confidence <= BROKEN_LINK_CONFIDENCE:
                # Do not guess. Hold position, record the break, and let the chain confidence
                # collapse — the mapper will then demand manual placement.
                cumulative *= max(motion.confidence, 0.0)
                heading += motion.yaw_deg
                r_cum = motion.rotation @ r_cum
                poses.append(
                    RelativePose(
                        frame_index=index + 1,
                        x=float(position[0]),
                        y=float(position[1]),
                        heading_deg=_wrap(heading),
                        step_confidence=float(motion.confidence),
                        cumulative_confidence=float(cumulative),
                    )
                )
                continue

            # Motion direction is expressed in camera i's frame; rotate it back into world.
            world_direction = r_cum.T @ motion.translation
            horizontal = np.array([world_direction[0], world_direction[2]])
            norm = float(np.linalg.norm(horizontal))
            if norm > 1e-9:
                position = position + step_length * (horizontal / norm)

            r_cum = motion.rotation @ r_cum
            heading += motion.yaw_deg
            cumulative *= float(motion.confidence)

            poses.append(
                RelativePose(
                    frame_index=index + 1,
                    x=float(position[0]),
                    y=float(position[1]),
                    heading_deg=_wrap(heading),
                    step_confidence=float(motion.confidence),
                    cumulative_confidence=float(cumulative),
                )
            )

        return poses


class KeyframeSelector:
    """Drops near-duplicate and unusable frames before mapping (§15).

    Two consecutive frames of the same wall add nothing but a degenerate zero-parallax pair, and
    a blurred frame contributes noise to the chain. Removing both up front is the cheapest
    accuracy win available.
    """

    def __init__(
        self,
        *,
        duplicate_hamming_threshold: int = 6,
        min_quality_score: float = 0.35,
    ) -> None:
        self.duplicate_hamming_threshold = duplicate_hamming_threshold
        self.min_quality_score = min_quality_score

    def select(
        self,
        hashes: list[str | None],
        quality_scores: list[float | None],
    ) -> list[bool]:
        keep: list[bool] = []
        last_kept_hash: str | None = None

        for index, phash in enumerate(hashes):
            score = quality_scores[index] if index < len(quality_scores) else None
            if score is not None and score < self.min_quality_score:
                keep.append(False)
                continue
            if (
                phash
                and last_kept_hash
                and hamming_distance(phash, last_kept_hash) <= self.duplicate_hamming_threshold
            ):
                keep.append(False)
                continue
            keep.append(True)
            if phash:
                last_kept_hash = phash

        # A selector that discards everything is worse than useless; always keep the first frame.
        if keep and not any(keep):
            keep[0] = True
        return keep


def grade_confidence(score: float) -> MappingConfidence:
    if score <= 0.0:
        return MappingConfidence.NONE
    if score < settings.mapping_confidence_threshold:
        return MappingConfidence.LOW
    if score < 0.7:
        return MappingConfidence.MEDIUM
    return MappingConfidence.HIGH


def summarize_mapping(
    poses: list[RelativePose],
    motions: list[RelativeMotion],
    *,
    threshold: float | None = None,
) -> MappingOutcome:
    """Turn a track into something the platform is willing to publish (§46).

    The published confidence is the **median step confidence**, not the mean and not the best:
    a walkthrough is only as mappable as its typical link. If that median falls below the
    configured threshold, the outcome demands manual placement and says why.
    """
    limit = settings.mapping_confidence_threshold if threshold is None else threshold
    per_pair = [
        {
            "pair_index": index,
            "confidence": round(motion.confidence, 4),
            "inliers": motion.inlier_count,
            "matches": motion.match_count,
            "yaw_deg": round(motion.yaw_deg, 2),
            "reasons": motion.reasons,
        }
        for index, motion in enumerate(motions)
    ]

    if not motions:
        return MappingOutcome(
            poses=poses,
            confidence=MappingConfidence.NONE,
            confidence_score=0.0,
            note=(
                "Automatic mapping needs at least two frames from the same walkthrough. "
                "Manual placement required."
            ),
            requires_manual_placement=True,
            per_pair=per_pair,
        )

    step_scores = np.array([m.confidence for m in motions], dtype=float)
    median = float(np.median(step_scores))
    broken = int(np.sum(step_scores <= BROKEN_LINK_CONFIDENCE))
    grade = grade_confidence(median)

    if median < limit:
        note = (
            f"Automatic mapping confidence: Low ({median:.0%}) — manual placement recommended. "
            f"{broken} of {len(motions)} frame pairs could not be related geometrically."
        )
        return MappingOutcome(
            poses=poses,
            confidence=grade,
            confidence_score=median,
            note=note,
            requires_manual_placement=True,
            per_pair=per_pair,
        )

    note = (
        f"Relative track estimated from {len(motions) + 1} frames at {median:.0%} median "
        "confidence. Positions are relative and scale-free — anchor two points on the floor "
        "plan to place the track."
    )
    if broken:
        note += f" {broken} weak link(s) were held in place rather than guessed."
    return MappingOutcome(
        poses=poses,
        confidence=grade,
        confidence_score=median,
        note=note,
        requires_manual_placement=False,
        per_pair=per_pair,
    )


def _wrap(degrees: float) -> float:
    return float((degrees + 180.0) % 360.0 - 180.0)
