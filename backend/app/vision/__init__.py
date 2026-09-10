"""Vision engine.

Self-contained: nothing in this package imports from ``app.api``, ``app.models`` or
``app.services``, so it can be tested — and eventually distributed — on its own.
"""

from app.vision.engine import VisionEngine, get_vision_engine
from app.vision.types import (
    FeatureSet,
    ImageInfo,
    MappingOutcome,
    MatchResult,
    QualityMetrics,
    RelativeMotion,
    RelativePose,
)

__all__ = [
    "FeatureSet",
    "ImageInfo",
    "MappingOutcome",
    "MatchResult",
    "QualityMetrics",
    "RelativeMotion",
    "RelativePose",
    "VisionEngine",
    "get_vision_engine",
]
