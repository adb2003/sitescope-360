from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from app.core.enums import DataProvenance, ProjectStatus
from app.schemas.common import ORMModel, SignedAsset

Name = Annotated[str, Field(min_length=1, max_length=200)]


# ---- project -----------------------------------------------------------------
class ProjectCreate(BaseModel):
    name: Name
    code: Annotated[str, Field(min_length=2, max_length=50, pattern=r"^[A-Za-z0-9._\- ]+$")]
    client: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=300)
    description: str | None = None
    status: ProjectStatus = ProjectStatus.ACTIVE
    start_date: date | None = None
    expected_completion_date: date | None = None
    project_manager_id: str | None = None
    expected_captures_per_week: Annotated[int, Field(ge=0, le=50)] = 2


class ProjectUpdate(BaseModel):
    name: Name | None = None
    client: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=300)
    description: str | None = None
    status: ProjectStatus | None = None
    start_date: date | None = None
    expected_completion_date: date | None = None
    project_manager_id: str | None = None
    expected_captures_per_week: Annotated[int, Field(ge=0, le=50)] | None = None


class ProjectOut(ORMModel):
    id: str
    name: str
    code: str
    client: str | None
    location: str | None
    description: str | None
    status: ProjectStatus
    start_date: date | None
    expected_completion_date: date | None
    project_manager_id: str | None
    expected_captures_per_week: int
    created_at: datetime
    updated_at: datetime


class ProjectSummary(ProjectOut):
    """Project plus the roll-ups the project list and dashboard need (§21)."""

    building_count: int = 0
    floor_count: int = 0
    zone_count: int = 0
    capture_session_count: int = 0
    frame_count: int = 0
    location_count: int = 0
    last_capture_at: datetime | None = None


# ---- building ----------------------------------------------------------------
class BuildingCreate(BaseModel):
    name: Name
    description: str | None = None


class BuildingUpdate(BaseModel):
    name: Name | None = None
    description: str | None = None


class BuildingOut(ORMModel):
    id: str
    project_id: str
    name: str
    description: str | None
    created_at: datetime


# ---- floor -------------------------------------------------------------------
class FloorCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    level: int = 0
    area_sqft: Annotated[float, Field(ge=0)] | None = None
    elevation_m: float | None = None


class FloorUpdate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    level: int | None = None
    area_sqft: Annotated[float, Field(ge=0)] | None = None
    elevation_m: float | None = None


class FloorOut(ORMModel):
    id: str
    building_id: str
    name: str
    level: int
    area_sqft: float | None
    elevation_m: float | None
    created_at: datetime


class FloorDetail(FloorOut):
    project_id: str
    building_name: str
    zone_count: int = 0
    location_count: int = 0
    capture_session_count: int = 0
    active_floor_plan_id: str | None = None
    last_capture_at: datetime | None = None


# ---- zone --------------------------------------------------------------------
class ZoneCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: str | None = None
    area_sqft: Annotated[float, Field(ge=0)] | None = None
    expected_capture_points: Annotated[int, Field(ge=0, le=1000)] = 1


class ZoneUpdate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    description: str | None = None
    area_sqft: Annotated[float, Field(ge=0)] | None = None
    expected_capture_points: Annotated[int, Field(ge=0, le=1000)] | None = None


class ZoneOut(ORMModel):
    id: str
    floor_id: str
    name: str
    description: str | None
    area_sqft: float | None
    expected_capture_points: int
    created_at: datetime


# ---- floor plan --------------------------------------------------------------
class FloorPlanOut(ORMModel):
    id: str
    floor_id: str
    name: str
    content_type: str
    width_px: int
    height_px: int
    file_size_bytes: int
    px_per_meter: float | None
    calibration: dict[str, Any] | None
    is_active: bool
    created_at: datetime
    asset: SignedAsset | None = None


class FloorPlanCalibration(BaseModel):
    """Two points on the raster and the real distance between them, in metres."""

    x1: float
    y1: float
    x2: float
    y2: float
    real_distance_m: Annotated[float, Field(gt=0)]


# ---- capture location --------------------------------------------------------
class CaptureLocationCreate(BaseModel):
    x: float
    y: float
    code: Annotated[str, Field(min_length=1, max_length=20)] | None = None
    label: str | None = Field(default=None, max_length=200)
    zone_id: str | None = None
    floor_plan_id: str | None = None
    provenance: DataProvenance = DataProvenance.MANUAL
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None


class CaptureLocationUpdate(BaseModel):
    x: float | None = None
    y: float | None = None
    label: str | None = Field(default=None, max_length=200)
    zone_id: str | None = None


class CaptureLocationOut(ORMModel):
    id: str
    floor_id: str
    zone_id: str | None
    floor_plan_id: str | None
    code: str
    label: str | None
    x: float
    y: float
    provenance: DataProvenance
    confidence: float | None
    created_at: datetime
    frame_count: int = 0
    latest_frame_id: str | None = None
    latest_captured_at: datetime | None = None


class NearestCaptureLocation(BaseModel):
    """One result of a nearest-location search, with how far away it actually was.

    The distance is returned rather than left implicit because "nearest" on an empty part of a
    floor plan is still *some* point, potentially the length of the building away. A caller that
    cannot see the distance has no way to tell a click on a capture point from a click on blank
    paper, and would silently jump the operator somewhere they did not ask to go.
    """

    location: CaptureLocationOut
    distance_px: float


class CaptureVisit(BaseModel):
    """One dated visit to a capture point."""

    frame_id: str
    session_id: str
    captured_at: datetime
    thumbnail: SignedAsset | None = None
    full: SignedAsset | None = None


class CapturesNearPoint(BaseModel):
    """What a click on blank floor plan resolves to: nearby points and their histories.

    The distance is returned for the same reason it is on a nearest search — the operator cannot
    otherwise tell a point they hit from one they merely came closest to.
    """

    location: CaptureLocationOut
    distance_px: float
    visits: list[CaptureVisit]


class CaptureScheduleIn(BaseModel):
    floor_id: str | None = None
    expected_per_week: Annotated[int, Field(ge=0, le=50)]
    grace_days: Annotated[int, Field(ge=0, le=30)] = 1
    notes: str | None = None


class CaptureScheduleOut(ORMModel):
    id: str
    project_id: str
    floor_id: str | None
    expected_per_week: int
    grace_days: int
    notes: str | None
