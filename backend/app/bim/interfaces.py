"""BIM interfaces — declared, deliberately not implemented (§30).

```
Revit / IFC  →  BIM Processing  →  Element graph  →  Shared coordinate system
                                                              ↓
                        Deviation report  ←  Computer vision  ←  Actual site 360
```

Nothing in this module is wired into the API, and none of it returns a fabricated answer. The
value it delivers today is a **checked seam**: the types below name exactly what a BIM comparison
will need, which is how we know the current schema can carry it.

What the database already provides for the future implementation:

* ``Floor.elevation_m`` — vertical placement against a project datum.
* ``FloorPlan.px_per_meter`` and ``FloorPlan.calibration`` — a human-entered pixel-to-metre
  scale, the anchor between drawing space and the real world.
* ``CaptureLocation.x/y`` plus that scale — capture positions expressible in metres.
* ``CaptureFrame.heading_deg`` — view orientation, needed to project an element into a panorama.
* ``DataProvenance`` on every positional field — so a deviation can state what it compared.

What is still missing, and must not be pretended away:

* a global site coordinate system (project base point / survey point);
* per-floor registration between drawing space and model space;
* element geometry, not just element identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from app.core.enums import DataProvenance


class BimNotImplementedError(NotImplementedError):
    """Raised by every BIM entry point in this build."""

    def __init__(self, feature: str) -> None:
        super().__init__(
            f"{feature} is a Phase 3 capability and is not implemented in this build. "
            "See docs/ROADMAP.md."
        )


@dataclass(frozen=True, slots=True)
class BimElement:
    """One model element, as a future importer would produce it."""

    external_id: str
    category: str
    name: str
    #: Metres, in the project coordinate system.
    x: float
    y: float
    z: float
    level: str | None = None
    scheduled_start: date | None = None
    scheduled_finish: date | None = None
    properties: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CoordinateAlignment:
    """Rigid transform from model coordinates to a floor plan's pixel space.

    Deliberately a similarity transform (scale + rotation + translation), matching
    :class:`app.vision.mapbuilder.SimilarityTransform`, so the two systems can be composed
    without introducing shear.
    """

    floor_id: str
    scale_px_per_m: float
    rotation_deg: float
    tx_px: float
    ty_px: float
    provenance: DataProvenance = DataProvenance.MANUAL
    rms_error_px: float | None = None


@dataclass(frozen=True, slots=True)
class Deviation:
    """A difference between planned and observed state."""

    element_external_id: str
    floor_id: str
    kind: str
    description: str
    confidence: float
    provenance: DataProvenance
    observed_frame_id: str | None = None


class BimSource(Protocol):
    """Reads a model file into elements. IFC first — it is an open standard."""

    def load(self, path: str) -> list[BimElement]: ...

    def supported_extensions(self) -> tuple[str, ...]: ...


class BimComparator(Protocol):
    """Compares expected elements against observed captures."""

    def compare(
        self,
        elements: list[BimElement],
        alignment: CoordinateAlignment,
        frame_ids: list[str],
    ) -> list[Deviation]: ...


class IfcBimSource:
    """Placeholder IFC reader.

    A real implementation would use ``ifcopenshell`` (LGPL) and is out of scope for the MVP.
    """

    def supported_extensions(self) -> tuple[str, ...]:
        return (".ifc",)

    def load(self, path: str) -> list[BimElement]:
        raise BimNotImplementedError("IFC import")


class GeometricBimComparator:
    def compare(
        self,
        elements: list[BimElement],
        alignment: CoordinateAlignment,
        frame_ids: list[str],
    ) -> list[Deviation]:
        raise BimNotImplementedError("BIM vs actual comparison")


def bim_status() -> dict[str, object]:
    """Machine-readable BIM readiness, surfaced by ``GET /api/system/capabilities``."""
    return {
        "implemented": False,
        "phase": 3,
        "interfaces_defined": [
            "BimSource",
            "BimComparator",
            "BimElement",
            "CoordinateAlignment",
            "Deviation",
        ],
        "schema_ready": [
            "floors.elevation_m",
            "floor_plans.px_per_meter",
            "floor_plans.calibration",
            "capture_locations.x",
            "capture_locations.y",
            "capture_frames.heading_deg",
        ],
        "still_required": [
            "project base point / survey point",
            "per-floor model-to-drawing registration",
            "element geometry (not only identity)",
        ],
        "note": (
            "No BIM endpoints are exposed. Interfaces exist so the data model can be verified "
            "against the future requirement, not to imply the feature works."
        ),
    }
