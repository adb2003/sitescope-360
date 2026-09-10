"""Map building — fitting a scale-free relative track onto a floor plan (§15).

The estimator produces a track whose *shape* is meaningful but whose position, rotation and
scale are arbitrary. Anchoring is therefore a similarity transform (uniform scale + rotation +
translation) fitted to user-supplied correspondences:

    two or more (track point ↔ floor-plan pixel) anchors  ⇒  the whole track is placed

This is solved in closed form with the Umeyama least-squares estimator. It is deliberately *not*
an affine or projective fit: allowing shear or perspective would let a bad anchor silently distort
the geometry into something that looks plausible and is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.vision.types import RelativePose

#: Fewer than two anchors leaves rotation and scale undetermined.
MIN_ANCHORS = 2


@dataclass(frozen=True, slots=True)
class SimilarityTransform:
    scale: float
    rotation_deg: float
    tx: float
    ty: float
    #: RMS residual in floor-plan pixels across the anchors used to fit.
    rms_error_px: float

    def apply(self, points: np.ndarray) -> np.ndarray:
        if points.size == 0:
            return np.zeros((0, 2))
        theta = np.deg2rad(self.rotation_deg)
        r = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
        return (self.scale * (np.asarray(points, dtype=float) @ r.T)) + np.array([self.tx, self.ty])

    def to_dict(self) -> dict[str, float]:
        return {
            "scale": round(self.scale, 6),
            "rotation_deg": round(self.rotation_deg, 3),
            "tx": round(self.tx, 3),
            "ty": round(self.ty, 3),
            "rms_error_px": round(self.rms_error_px, 3),
        }


@dataclass(frozen=True, slots=True)
class MapFit:
    transform: SimilarityTransform | None
    placed: list[dict[str, float]]
    note: str
    ok: bool


class MapBuilder:
    def fit_transform(self, source: np.ndarray, target: np.ndarray) -> SimilarityTransform | None:
        """Umeyama similarity fit: ``target ≈ s·R·source + t``."""
        src = np.asarray(source, dtype=float)
        dst = np.asarray(target, dtype=float)
        if src.shape != dst.shape or src.shape[0] < MIN_ANCHORS:
            return None

        src_mean = src.mean(axis=0)
        dst_mean = dst.mean(axis=0)
        src_centred = src - src_mean
        dst_centred = dst - dst_mean

        src_variance = float((src_centred**2).sum() / src.shape[0])
        if src_variance < 1e-12:
            # Every anchor maps to the same track point — scale is undetermined.
            return None

        covariance = (dst_centred.T @ src_centred) / src.shape[0]
        try:
            u, singular, vt = np.linalg.svd(covariance)
        except np.linalg.LinAlgError:
            return None

        correction = np.eye(2)
        if np.linalg.det(u @ vt) < 0:
            # Forbid reflection: a mirrored walkthrough is never the right answer.
            correction[1, 1] = -1.0
        rotation = u @ correction @ vt
        scale = float((singular * np.diag(correction)).sum() / src_variance)
        translation = dst_mean - scale * (rotation @ src_mean)

        predicted = (scale * (src @ rotation.T)) + translation
        rms = float(np.sqrt(np.mean(np.sum((predicted - dst) ** 2, axis=1))))

        return SimilarityTransform(
            scale=scale,
            rotation_deg=float(np.degrees(np.arctan2(rotation[1, 0], rotation[0, 0]))),
            tx=float(translation[0]),
            ty=float(translation[1]),
            rms_error_px=rms,
        )

    def place_track(
        self,
        poses: list[RelativePose],
        anchors: list[tuple[int, float, float]],
    ) -> MapFit:
        """Place a relative track on a floor plan.

        ``anchors`` are ``(frame_index, plan_x_px, plan_y_px)`` triples supplied by whoever
        dragged those points in the floor-plan editor.
        """
        if len(poses) < 1:
            return MapFit(None, [], "No estimated positions to place.", ok=False)

        if len(anchors) < MIN_ANCHORS:
            return MapFit(
                None,
                [],
                (
                    f"Place at least {MIN_ANCHORS} capture points manually on the floor plan to "
                    "anchor the estimated track. Relative positions alone have no scale, "
                    "rotation or origin."
                ),
                ok=False,
            )

        by_index = {pose.frame_index: pose for pose in poses}
        source: list[list[float]] = []
        target: list[list[float]] = []
        for frame_index, plan_x, plan_y in anchors:
            pose = by_index.get(frame_index)
            if pose is None:
                continue
            source.append([pose.x, pose.y])
            target.append([plan_x, plan_y])

        if len(source) < MIN_ANCHORS:
            return MapFit(
                None,
                [],
                "Anchors do not correspond to any estimated frame position.",
                ok=False,
            )

        transform = self.fit_transform(np.array(source), np.array(target))
        if transform is None:
            return MapFit(
                None,
                [],
                (
                    "Anchor points are collinear or coincident in the estimated track, so scale "
                    "and rotation cannot be resolved. Pick anchors that are far apart."
                ),
                ok=False,
            )

        track = np.array([[pose.x, pose.y] for pose in poses])
        placed_xy = transform.apply(track)
        placed = [
            {
                "frame_index": float(pose.frame_index),
                "x": float(placed_xy[i, 0]),
                "y": float(placed_xy[i, 1]),
                "confidence": float(pose.cumulative_confidence),
            }
            for i, pose in enumerate(poses)
        ]

        note = (
            f"Track placed from {len(source)} anchors; residual {transform.rms_error_px:.1f}px. "
            "Positions are estimated — review before relying on them."
        )
        if transform.rms_error_px > 60:
            note = (
                f"Anchor fit is poor (residual {transform.rms_error_px:.1f}px). The estimated "
                "track does not match the anchors — manual placement is recommended."
            )
            return MapFit(transform, placed, note, ok=False)

        return MapFit(transform, placed, note, ok=True)

    def normalize_track(self, poses: list[RelativePose]) -> list[dict[str, float]]:
        """Scale a track into a unit box for preview before any anchoring exists.

        Lets the UI show "here is the shape we think you walked" without implying it knows where
        on the plan that shape belongs.
        """
        if not poses:
            return []
        points = np.array([[p.x, p.y] for p in poses])
        span = points.max(axis=0) - points.min(axis=0)
        extent = float(max(span.max(), 1e-6))
        origin = points.min(axis=0)
        unit = (points - origin) / extent
        return [
            {
                "frame_index": float(pose.frame_index),
                "x": float(unit[i, 0]),
                "y": float(unit[i, 1]),
                "heading_deg": float(pose.heading_deg),
                "confidence": float(pose.cumulative_confidence),
            }
            for i, pose in enumerate(poses)
        ]
