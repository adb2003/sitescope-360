"""Value objects exchanged between vision components.

Kept free of SQLAlchemy and Pydantic so the vision package can be tested — and one day
extracted — without the web application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core.enums import MappingConfidence, ProjectionType, QualityVerdict


@dataclass(slots=True)
class ImageInfo:
    width: int
    height: int
    channels: int
    projection: ProjectionType
    aspect_ratio: float
    #: How strongly the geometry suggests equirectangular, 0–1. Reported, never asserted.
    equirect_confidence: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QualityMetrics:
    """Normalised 0–1 scores plus the raw measurements behind them."""

    quality_score: float
    blur_score: float
    brightness_score: float
    contrast_score: float
    resolution_score: float
    completeness_score: float
    verdict: QualityVerdict
    is_usable: bool
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_api_dict(self) -> dict[str, Any]:
        """The §10 response shape."""
        return {
            "quality_score": round(self.quality_score, 3),
            "blur_score": round(self.blur_score, 3),
            "brightness_score": round(self.brightness_score, 3),
            "is_usable": self.is_usable,
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class FeatureSet:
    """ORB keypoints and descriptors for one frame."""

    #: (N, 2) pixel coordinates.
    keypoints: np.ndarray
    #: (N, 32) uint8 ORB descriptors.
    descriptors: np.ndarray
    image_width: int
    image_height: int
    projection: ProjectionType

    @property
    def count(self) -> int:
        return int(self.keypoints.shape[0]) if self.keypoints.size else 0


@dataclass(slots=True)
class MatchResult:
    """Verified correspondences between two frames."""

    #: (M, 2) pixel coords in frame A / frame B, after ratio test and geometric verification.
    points_a: np.ndarray
    points_b: np.ndarray
    #: Matches surviving the Lowe ratio test, before geometric verification.
    raw_match_count: int
    inlier_count: int

    @property
    def inlier_ratio(self) -> float:
        if self.raw_match_count == 0:
            return 0.0
        return self.inlier_count / self.raw_match_count


@dataclass(slots=True)
class RelativeMotion:
    """Scale-free relative motion between two frames.

    ``translation`` is a **unit direction**, not a displacement: a single moving camera cannot
    recover metric scale. ``yaw_deg`` is the heading change about the vertical axis.
    """

    translation: np.ndarray  # (3,) unit vector in frame-A camera coordinates
    rotation: np.ndarray  # (3, 3)
    yaw_deg: float
    confidence: float
    inlier_count: int
    match_count: int
    reasons: list[str] = field(default_factory=list)

    @property
    def is_reliable(self) -> bool:
        return self.confidence > 0.0 and self.inlier_count >= 20


@dataclass(slots=True)
class RelativePose:
    """Dead-reckoned pose of one frame relative to the first frame of its sequence."""

    frame_index: int
    x: float
    y: float
    heading_deg: float
    step_confidence: float
    #: Product of every step confidence up to here — drift makes later frames less trustworthy.
    cumulative_confidence: float


@dataclass(slots=True)
class MappingOutcome:
    """The result the platform is willing to publish (§14, §46)."""

    poses: list[RelativePose]
    confidence: MappingConfidence
    confidence_score: float
    note: str
    requires_manual_placement: bool
    per_pair: list[dict[str, Any]] = field(default_factory=list)
