"""AI engine interfaces (§31).

```
AIEngine
 ├── ImageQuality      implemented  → app.vision.quality
 ├── FeatureMatching   implemented  → app.vision.features
 ├── ObjectDetection   NOT implemented (Phase 3)
 ├── DefectDetection   NOT implemented (Phase 3)
 ├── ProgressDetection NOT implemented (Phase 3)
 └── BIMComparison     NOT implemented (Phase 3)
```

The unimplemented modules exist here as *interfaces only*. They raise
:class:`NotImplementedError` rather than returning plausible-looking numbers, and
``AIEngine.capabilities()`` reports them as unavailable so the UI can say
"Progress analysis requires additional historical captures" instead of inventing a percentage
(§46).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from app.core.enums import DataProvenance
from app.vision.engine import VisionEngine, get_vision_engine


@dataclass(frozen=True, slots=True)
class Detection:
    label: str
    confidence: float
    #: Normalised (x, y, w, h) in the source frame.
    bbox: tuple[float, float, float, float]
    provenance: DataProvenance = DataProvenance.AI_PREDICTED


@dataclass(frozen=True, slots=True)
class UnavailableAnalysis:
    """Returned instead of a fabricated result when a module cannot run."""

    reason: str
    required: list[str]
    available: bool = False


class ObjectDetector(Protocol):
    def detect(self, image: np.ndarray) -> list[Detection]: ...


class DefectDetector(Protocol):
    def detect(self, image: np.ndarray) -> list[Detection]: ...


class ProgressDetector(Protocol):
    def compare(
        self, baseline: np.ndarray, current: np.ndarray
    ) -> UnavailableAnalysis | dict[str, Any]: ...


class NotImplementedObjectDetector:
    """Placeholder. Phase 3 will substitute a trained detector behind this same interface."""

    def detect(self, image: np.ndarray) -> list[Detection]:
        raise NotImplementedError(
            "Object detection is a Phase 3 capability and is not implemented. See docs/ROADMAP.md."
        )


class NotImplementedDefectDetector:
    def detect(self, image: np.ndarray) -> list[Detection]:
        raise NotImplementedError(
            "Defect detection is a Phase 3 capability and is not implemented. "
            "Use annotations to record defects manually."
        )


class HistoricalProgressDetector:
    """Progress detection needs a trained model *and* a capture history.

    Rather than pretend, this reports precisely what is missing. The visual-change signal it can
    honestly provide — verified feature overlap between two dates at the same location — is
    available through :meth:`VisionEngine.similarity` and is labelled a *similarity measurement*,
    not a progress percentage.
    """

    MIN_HISTORICAL_CAPTURES = 3

    def compare(
        self, baseline: np.ndarray, current: np.ndarray
    ) -> UnavailableAnalysis | dict[str, Any]:
        return UnavailableAnalysis(
            reason=(
                "Progress analysis requires a trained progress model, which is a Phase 3 "
                "capability."
            ),
            required=[
                "trained progress-detection model",
                f"at least {self.MIN_HISTORICAL_CAPTURES} historical captures at the location",
                "BIM or schedule reference for expected state",
            ],
        )


class AIEngine:
    """Aggregates what is implemented and what is not, honestly."""

    def __init__(self, vision: VisionEngine | None = None) -> None:
        self.vision = vision or get_vision_engine()
        self.object_detection = NotImplementedObjectDetector()
        self.defect_detection = NotImplementedDefectDetector()
        self.progress_detection = HistoricalProgressDetector()

    @property
    def image_quality(self) -> Any:
        return self.vision.quality_analyzer

    def capabilities(self) -> dict[str, Any]:
        capabilities = self.vision.describe_capabilities()
        capabilities["bim_comparison"] = {
            "implemented": False,
            "phase": 3,
            "note": "Interfaces defined in app/bim/. No implementation, by design.",
        }
        return capabilities


_ai_engine: AIEngine | None = None


def get_ai_engine() -> AIEngine:
    global _ai_engine
    if _ai_engine is None:
        _ai_engine = AIEngine()
    return _ai_engine
